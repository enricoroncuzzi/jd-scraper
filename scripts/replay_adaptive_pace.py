"""Deterministic replay of the adaptive LinkedIn pace ladder over a pace_events CSV.

    python scripts/replay_adaptive_pace.py <csv> [--since YYYY-MM-DD] [--start-level N]
        [--demand N] [--llm N] [--disabled] [--connection-errors-at HH:MM,...] [--json]

The replay uses the recorded request timing (75 s history), so it cannot show a faster
level's throughput; the throughput clause is skipped and the limitation is printed. It
builds the shipped eight-level policy, but the recorded traces are far shorter than the
above-3x gates need (600 requests per level, one climb per Rome day), so a replay never
exercises them. No database and no network are touched.
"""
import argparse
import csv
import json
import os
import sys
from dataclasses import replace
from datetime import date
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.linkedin.adaptive import (  # noqa: E402
    Signals, event_from_csv_row, replay,
)
from src.linkedin.schedule import AdaptivePolicy  # noqa: E402

ROME = ZoneInfo("Europe/Rome")
LIMITATION = ("replay uses the recorded 75 s timing, so the throughput clause "
              "is not evaluated")


def _load(path: str, since: date | None) -> list:
    with open(path) as handle:
        events = [event_from_csv_row(row) for row in csv.DictReader(handle)]
    if since is not None:
        events = [event for event in events if event.at.date() >= since]
    return events


def _mark_connections(events, marks: set[str]):
    if not marks:
        return events
    marked = []
    for event in events:
        stamp = event.at.astimezone(ROME).strftime("%H:%M")
        marked.append(replace(event, block="connection") if stamp in marks else event)
    return marked


def _iso(value):
    return value.isoformat() if value is not None else None


def _payload(result, events):
    moves = [[_iso(at), kind, source, target, reason]
             for at, kind, source, target, reason in result.moves]
    return {
        "requests": len(events),
        "first": _iso(events[0].at) if events else None,
        "last": _iso(events[-1].at) if events else None,
        "levels_reached": result.levels_reached,
        "moves": moves,
        "climbs": [move for move in moves if move[1] == "climb"],
        "drops": [[_iso(at), source, target, reason]
                  for at, source, target, reason in result.drops],
        "brakes": [[_iso(at), reason] for at, reason in result.brakes],
        "pauses": [day.isoformat() for day in result.pauses],
        "final_level": result.final_state["level"],
        "limitation": LIMITATION,
    }


def _print_text(payload):
    print(f"Replay: {payload['requests']} requests, {payload['first']} -> {payload['last']}")
    print(f"Levels reached: {payload['levels_reached']}")
    print(f"Final level: {payload['final_level']}")
    for move in payload["moves"]:
        print(f"  {move[0]} {move[1]} {move[2]} -> {move[3]} ({move[4]})")
    print(f"Drops: {len(payload['drops'])}")
    for drop in payload["drops"]:
        print(f"  {drop[0]} {drop[1]} -> {drop[2]} ({drop[3]})")
    print(f"Brakes: {len(payload['brakes'])}; pauses: {payload['pauses']}")
    print(f"limitation: {LIMITATION}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Replay the adaptive LinkedIn pace ladder")
    parser.add_argument("csv")
    parser.add_argument("--since", type=date.fromisoformat)
    parser.add_argument("--start-level", type=int, default=1)
    parser.add_argument("--demand", type=int, default=5000)
    parser.add_argument("--llm", type=int, default=100)
    parser.add_argument("--disabled", action="store_true")
    parser.add_argument("--connection-errors-at", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    events = _load(args.csv, args.since)
    marks = {value.strip() for value in args.connection_errors_at.split(",") if value.strip()}
    events = _mark_connections(events, marks)
    policy = AdaptivePolicy(enabled=not args.disabled)
    signals = Signals(demand_waiting=args.demand, llm_requests_today=args.llm,
                      llm_stop_requests=800, refreshed_at=None)
    result = replay(events, policy, signals=signals, start_level=args.start_level,
                    throughput_check=False)
    payload = _payload(result, events)
    if args.json:
        print(json.dumps(payload))
    else:
        _print_text(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
