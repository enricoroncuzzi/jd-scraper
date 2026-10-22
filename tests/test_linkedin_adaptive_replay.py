import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.linkedin.adaptive import (
    ReplayEvent, Signals, blank_state, event_from_csv_row, replay,
)
from src.linkedin.schedule import AdaptivePolicy

POLICY = AdaptivePolicy(enabled=True)
SIGNALS = Signals(demand_waiting=5000, llm_requests_today=100, llm_stop_requests=800,
                  llm_day=datetime(2026, 10, 20, tzinfo=timezone.utc).date(),
                  refreshed_at=None)
START = datetime(2026, 10, 20, 3, 0, tzinfo=timezone.utc)
FIXTURE = str(Path(__file__).parent / "fixtures" / "pace_events_2026-10-20.csv")


def event(offset, outcome, block=None, day=None, wait_seconds=None):
    return ReplayEvent(START + timedelta(minutes=offset), outcome, block=block, day=day,
                       wait_seconds=wait_seconds)


def test_the_replay_drops_two_levels_on_four_refusals_in_a_row():
    result = replay([event(m, "refused") for m in range(4)], POLICY,
                    signals=SIGNALS, start_level=4)
    assert result.drops == [(event(3, "refused").at, 4, 2, "protect: 4 refusals in a row")]
    assert result.final_state["level"] == 2


def test_a_partial_window_does_not_drop_below_the_minimum_sample():
    events = [event(i, "refused" if i % 2 == 0 else "clean") for i in range(9)]
    result = replay(events, POLICY, signals=SIGNALS, start_level=4)
    assert result.drops == [] and result.final_state["level"] == 4


def test_the_replay_climbs_only_after_200_clean_requests():
    result = replay([event(m, "clean") for m in range(200)], POLICY,
                    signals=SIGNALS, start_level=1)
    assert [m[1] for m in result.moves if m[1] == "climb"] == ["climb"]
    assert result.final_state["level"] == 2


def test_the_replay_does_not_climb_on_a_nine_percent_refusal_rate():
    events = [event(m, "refused" if m % 11 == 0 else "clean") for m in range(200)]
    result = replay(events, POLICY, signals=SIGNALS, start_level=1)
    assert not [m for m in result.moves if m[1] == "climb"]


def test_a_drop_clears_the_window():
    result = replay([event(m, "refused") for m in range(4)], POLICY,
                    signals=SIGNALS, start_level=4)
    assert result.final_state["trailing"] == []


def test_the_clean_day_replay_halves_the_cooldown():
    start = {**blank_state(POLICY), "active": True, "level": 1, "day": "2026-10-19",
             "day_requests": 100, "day_drops": 0, "day_brakes": 0}
    start["levels"]["3"]["cooldown_hours"] = 8.0
    events = [ReplayEvent(datetime(2026, 10, 20, 0, 1, tzinfo=timezone.utc)
                          + timedelta(seconds=75 * i), "clean",
                          day=datetime(2026, 10, 20).date()) for i in range(101)]
    result = replay(events, POLICY, signals=SIGNALS, start_state=start)
    assert result.final_state["levels"]["3"]["cooldown_hours"] == 4.0


def test_the_brake_replay_brakes_pauses_and_resumes():
    events = [
        event(0, "clean"),
        event(1, "neutral", block="http_999"),
        event(70, "neutral", block="http_999"),
        event(24 * 60 + 1, "clean", day=datetime(2026, 10, 21).date()),
    ]
    result = replay(events, POLICY, signals=SIGNALS, start_level=3)
    assert result.brakes and result.brakes[0][1] == "http_999"
    assert datetime(2026, 10, 20).date() in result.pauses
    assert result.final_state["level"] == 1 and result.final_state["paused"] is None


def test_the_recorded_2026_10_20_morning_drops_fast_from_level_three():
    rows = list(csv.DictReader(Path(FIXTURE).open()))
    result = replay([event_from_csv_row(row) for row in rows], POLICY,
                    signals=SIGNALS, start_level=3)
    drops = [(d[0].strftime("%H:%M"), d[1], d[2]) for d in result.drops]
    assert ("04:37", 3, 2) in drops
    assert ("05:00", 2, 1) in drops
    assert result.final_state["level"] == 1


def test_the_replay_respects_the_demand_and_llm_gates():
    clean = [event(m, "clean") for m in range(200)]
    low = Signals(demand_waiting=500, llm_requests_today=100, llm_stop_requests=800,
                  llm_day=START.date(), refreshed_at=None)
    high = Signals(demand_waiting=5000, llm_requests_today=100, llm_stop_requests=800,
                   llm_day=START.date(), refreshed_at=None)
    no_headroom = Signals(demand_waiting=5000, llm_requests_today=800,
                          llm_stop_requests=800, llm_day=START.date(), refreshed_at=None)
    assert replay(clean, POLICY, signals=low, start_level=1).final_state["level"] == 1
    assert replay(clean, POLICY, signals=high, start_level=1).final_state["level"] == 2
    assert replay(clean, POLICY, signals=no_headroom, start_level=1).final_state["level"] == 1


def test_the_replay_maps_a_max_wait_refusal_to_the_brake():
    result = replay([event(0, "refused", wait_seconds=1800.0)], POLICY,
                    signals=SIGNALS, start_level=3, max_wait_seconds=1800)
    assert result.final_state["brake"] is not None
    assert result.final_state["level"] == 1


def test_event_from_csv_row_maps_the_recorded_outcomes():
    assert event_from_csv_row({"at": "2026-10-20 04:00:00+02", "outcome": "ok",
                               "status": "200"}).outcome == "clean"
    assert event_from_csv_row({"at": "2026-10-20 04:00:00+02", "outcome": "refused",
                               "status": "429"}).outcome == "refused"
    assert event_from_csv_row({"at": "2026-10-20 04:00:00+02", "outcome": "neutral",
                               "status": "404"}).outcome == "neutral"
    assert event_from_csv_row({"at": "2026-10-20 04:00:00+02", "outcome": "neutral",
                               "status": "404"}).block is None


def test_the_cli_prints_json_summary(capsys):
    from scripts.replay_adaptive_pace import main
    assert main([FIXTURE, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["requests"] == 162 and payload["final_level"] == 1
    assert "limitation" in payload


def test_the_cli_start_level_reports_the_recorded_drops(capsys):
    from scripts.replay_adaptive_pace import main
    assert main([FIXTURE, "--start-level", "3", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [d[3] for d in payload["drops"]] == ["protect: trailing 20 refused 30%",
                                                "protect: trailing 20 refused 60%"]


def test_the_cli_flags_prove_their_effect(tmp_path, capsys):
    from scripts.replay_adaptive_pace import main
    path = tmp_path / "clean.csv"
    base = datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc)
    rows = ["id,at,service,kind,outcome,status,factor,requests_today,spacing_seconds,wait_seconds"]
    for i in range(250):
        at = (base + timedelta(seconds=75 * i)).isoformat()
        rows.append(f"{i + 1},{at},process,description,ok,200,,,75,")
    path.write_text("\n".join(rows) + "\n")

    assert main([str(path), "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["climbs"]) == 1
    assert main([str(path), "--demand", "500", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["climbs"] == []
    assert main([str(path), "--llm", "800", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["climbs"] == []
    assert main([str(path), "--disabled", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["final_level"] == 1 and payload["climbs"] == []
    assert main([str(path), "--since", "2026-10-21", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["requests"] == 0


def test_the_cli_connection_errors_at_brakes_on_three_and_not_two(tmp_path, capsys):
    from scripts.replay_adaptive_pace import main
    path = tmp_path / "pace.csv"
    path.write_text(
        "id,at,service,kind,outcome,status,factor,requests_today,spacing_seconds,wait_seconds\n"
        "1,2026-10-20 10:00:00+02,process,description,neutral,,,,,\n"
        "2,2026-10-20 10:01:00+02,process,description,neutral,,,,,\n"
        "3,2026-10-20 10:02:00+02,process,description,neutral,,,,,\n")
    assert main([str(path), "--connection-errors-at", "10:00,10:01,10:02", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["brakes"]
    assert main([str(path), "--connection-errors-at", "10:00,10:01", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["brakes"] == []
