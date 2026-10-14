"""One service per extra source: collect-freehire, collect-himalayas.

    python -m src.sources.service freehire --sync-slices   # create the slices, then exit
    python -m src.sources.service freehire --once          # walk one due slice, then exit
    python -m src.sources.service freehire                 # run forever (systemd)

A source ships `enabled: false` in config/sources.json and refuses to run until the
captain switches it on, one source at a time with days of comparison between.
"""
import argparse
import sys

import requests
from dotenv import load_dotenv

from src.jobstore import heartbeat
from src.jobstore.db import connect, store_url, utcnow
from src.jobstore.schema import init_store
from src.linkedin.catalog import sync_slices
from src.linkedin.walk import claim_due_slice
from src.services.runner import Tick, run_service
from src.sources import freehire, himalayas
from src.sources.base import (
    JsonClient, SourceConfig, SourceError, SourceWalk, finish_source_walk, load_source_config,
)

MODULES = {"freehire": freehire, "himalayas": himalayas}

# A daily timer fires at the same clock time every day, but a walk finishes a few seconds
# after it started. Scheduling the next walk an hour short of a full interval keeps the slice
# due when the next day's timer fires instead of skipping every other day.
DUE_SLACK_SECONDS = 3600


def collect_source_once(conn, name: str, cfg: SourceConfig, client: JsonClient, *,
                        now_fn=utcnow) -> Tick:
    started = now_fn()
    row = claim_due_slice(conn, started, name)
    if row is None:
        return Tick(False, {"due_slices": 0})
    try:
        walk = MODULES[name].walk_slice(conn, client, cfg, row, started)
    except SourceError as exc:
        conn.rollback()
        print(f"[collect-{name}] {row.role} / {row.area}: {exc}")
        walk = SourceWalk(0, 0, 0, "error", row.resume_start)
    interval_seconds = cfg.interval_hours * 3600
    finish_source_walk(conn, row, walk, started, now_fn(),
                       max(interval_seconds - DUE_SLACK_SECONDS, interval_seconds // 2))
    conn.commit()
    print(f"[collect-{name}] {row.role} / {row.area}: {walk.pages} pages, {walk.cards_seen} hits, "
          f"{walk.new_jobs} new, stop={walk.stop_reason}")
    return Tick(True, {"slice": f"{row.role} / {row.area}", "stop_reason": walk.stop_reason,
                       "new_jobs": walk.new_jobs})


def collect_due_slices(conn, name: str, cfg: SourceConfig, client: JsonClient, *,
                       now_fn=utcnow) -> list[Tick]:
    """Drain every slice that is due right now, for a oneshot timer run.

    The daily timer starts the collector once; a single `collect_source_once` would walk
    only the first due slice, so a oneshot run drains them all before exiting. It leaves a
    heartbeat the watchdog reads between runs, marked error if any slice failed."""
    service = f"collect-{name}"
    heartbeat.start(conn, service, now=now_fn())
    conn.commit()
    ticks: list[Tick] = []
    error = False
    try:
        while True:
            tick = collect_source_once(conn, name, cfg, client, now_fn=now_fn)
            if not tick.did_work:
                break
            ticks.append(tick)
            error = error or tick.detail.get("stop_reason") == "error"
            heartbeat.beat(conn, service, "error" if error else "ok",
                           {"slices": len(ticks), "new_jobs": sum(t.detail.get("new_jobs", 0) for t in ticks)},
                           now=now_fn())
            conn.commit()
    except Exception as exc:  # noqa: BLE001 - a crash must still leave a truthful error beat
        conn.rollback()
        heartbeat.beat(conn, service, "error", {"crashed": type(exc).__name__}, now=now_fn())
        conn.commit()
        raise
    if not ticks:
        heartbeat.beat(conn, service, "idle", {"slices": 0}, now=now_fn())
        conn.commit()
    return ticks


def main(argv: list[str] | None = None, *, get=requests.get) -> int:
    parser = argparse.ArgumentParser(description="Source collector service")
    parser.add_argument("source", choices=sorted(MODULES))
    parser.add_argument("--sync-slices", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    load_dotenv()
    cfg = load_source_config(args.source)
    if not cfg.enabled:
        print(f"[collect-{args.source}] disabled in config/sources.json; not running")
        return 0
    client = JsonClient(cfg, get=get)
    specs = MODULES[args.source].build_slices(cfg)

    if args.sync_slices or args.once:
        conn = connect(store_url())
        failed = False
        try:
            init_store(conn)
            counts = sync_slices(conn, specs, source=args.source)
            conn.commit()
            print(f"[collect-{args.source}] slices: {counts}")
            if args.once:
                ticks = collect_due_slices(conn, args.source, cfg, client)
                failed = bool(ticks) and all(t.detail.get("stop_reason") == "error" for t in ticks)
        finally:
            conn.close()
        return 1 if failed else 0

    synced = {"done": False}

    def tick(conn) -> Tick:
        if not synced["done"]:
            sync_slices(conn, specs, source=args.source)
            conn.commit()
            synced["done"] = True
        return collect_source_once(conn, args.source, cfg, client)

    return run_service(f"collect-{args.source}", tick, connect_fn=lambda: connect(store_url()),
                       idle_seconds=300)


if __name__ == "__main__":
    sys.exit(main())
