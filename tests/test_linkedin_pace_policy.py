from datetime import date, datetime, timedelta, timezone

import pytest

from src.linkedin.pace import (
    PaceState, description_run_cap, evaluate_gate, must_yield, next_spacing,
    refusal_wait_seconds, roll_day,
)
from src.linkedin.schedule import (
    PacePolicy, RefusalPolicy, ScheduleConfig, start_spacing_seconds,
)

WED_NOON = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)  # 12:00 Rome, Wednesday
SAT_NOON = datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc)
NOW_TS = WED_NOON.timestamp()
MID = lambda a, b: (a + b) / 2  # noqa: E731 - no jitter
RUN_CAP = description_run_cap(ScheduleConfig().pace.description_turn_share)


def test_the_fixed_spacing_is_75s_on_every_day():
    cfg = ScheduleConfig()
    assert start_spacing_seconds(cfg) == 75
    assert next_spacing(cfg, WED_NOON, MID) == pytest.approx(75.0)
    assert next_spacing(cfg, SAT_NOON, MID) == pytest.approx(75.0)


def test_each_spacing_is_the_base_jittered_by_twenty_percent():
    cfg, low, high = ScheduleConfig(), (lambda a, b: a), (lambda a, b: b)
    assert next_spacing(cfg, WED_NOON, low) == pytest.approx(60.0)
    assert next_spacing(cfg, WED_NOON, high) == pytest.approx(90.0)
    assert next_spacing(cfg, SAT_NOON, low) == pytest.approx(60.0)
    assert next_spacing(cfg, SAT_NOON, high) == pytest.approx(90.0)
    # Nothing clamps a configured base from below: there is no cap to defend.
    slow = ScheduleConfig(pace=PacePolicy(start_spacing_seconds=1.0))
    assert next_spacing(slow, WED_NOON, low) == pytest.approx(0.8)
    assert next_spacing(slow, SAT_NOON, low) == pytest.approx(0.8)


def test_a_stored_base_or_nudge_from_the_old_code_cannot_slow_the_fixed_pace():
    # The live row may still carry the old 99/133 s bases and nudge x3: PaceState has no
    # such fields any more, and next_spacing reads only config, so a stale row is inert.
    assert not hasattr(PaceState(WED_NOON), "nudge")
    assert not hasattr(PaceState(WED_NOON), "weekday_spacing")
    assert next_spacing(ScheduleConfig(), WED_NOON, MID) == pytest.approx(75.0)


def test_the_refusal_wait_starts_at_60s_doubles_and_stops_at_30_minutes():
    policy = RefusalPolicy()
    waits = [refusal_wait_seconds(n, None, policy, MID, jitter=0.2) for n in (1, 2, 3, 6, 9)]
    assert waits == [60, 120, 240, 1800, 1800]
    assert refusal_wait_seconds(5, None, policy, lambda a, b: b, jitter=0.2) == pytest.approx(960 * 1.2)


def test_retry_after_wins_but_is_capped_at_thirty_minutes():
    policy = RefusalPolicy()
    assert refusal_wait_seconds(1, 90.0, policy, MID, jitter=0.2) == 90
    assert refusal_wait_seconds(1, 0.0, policy, MID, jitter=0.2) == 0
    assert refusal_wait_seconds(1, 86400.0, policy, MID, jitter=0.2) == 1800


def test_the_description_share_becomes_a_run_of_consecutive_description_turns():
    assert description_run_cap(2 / 3) == 2
    assert description_run_cap(0.5) == 1
    assert description_run_cap(3 / 4) == 3


def _take(state, kind):
    """Apply just the turn-order half of PaceBudget._record."""
    state.description_run = state.description_run + 1 if kind == "description" else 0
    state.last_kind = kind


def test_descriptions_get_two_of_every_three_turns_when_both_kinds_wait():
    state = PaceState(WED_NOON, wanting={"search": NOW_TS - 5, "description": NOW_TS - 5})
    taken = []
    for _ in range(9):
        if not must_yield(state, "description", NOW_TS, run_cap=RUN_CAP):
            taken.append("description")
            _take(state, "description")
        else:
            assert not must_yield(state, "search", NOW_TS, run_cap=RUN_CAP)
            taken.append("search")
            _take(state, "search")
    assert taken == ["description", "description", "search"] * 3


def test_a_search_waits_its_turn_while_descriptions_are_under_their_share():
    state = PaceState(WED_NOON, description_run=1, wanting={"search": NOW_TS - 5,
                                                            "description": NOW_TS - 5})
    assert must_yield(state, "search", NOW_TS, run_cap=2) is True
    assert must_yield(state, "description", NOW_TS, run_cap=2) is False


def test_a_description_yields_once_its_run_reaches_the_cap():
    state = PaceState(WED_NOON, description_run=2, wanting={"search": NOW_TS - 5,
                                                            "description": NOW_TS - 5})
    assert must_yield(state, "description", NOW_TS, run_cap=2) is True
    assert must_yield(state, "search", NOW_TS, run_cap=2) is False


def test_a_lone_waiting_kind_always_takes_the_turn():
    description_only = PaceState(WED_NOON, description_run=99, wanting={"description": NOW_TS - 5})
    assert must_yield(description_only, "description", NOW_TS, run_cap=2) is False
    search_only = PaceState(WED_NOON, wanting={"search": NOW_TS - 5})
    assert must_yield(search_only, "search", NOW_TS, run_cap=2) is False


def test_a_stale_marker_is_ignored():
    state = PaceState(WED_NOON, wanting={"search": NOW_TS - 31})
    assert must_yield(state, "description", NOW_TS, run_cap=2) is False


def test_the_gate_stays_open_all_day_with_no_closed_window():
    cfg = ScheduleConfig()
    assert evaluate_gate(cfg, WED_NOON, requests_today=0).reason == "open"
    assert evaluate_gate(cfg, WED_NOON, requests_today=1400).reason == "open"
    gate = evaluate_gate(cfg, WED_NOON, requests_today=10000)
    assert gate.open and gate.reason == "open" and gate.requests_today == 10000
    assert not hasattr(gate, "window") and not hasattr(gate, "cap")
    night = datetime(2026, 10, 14, 1, 0, tzinfo=timezone.utc)  # 03:00 Rome
    assert evaluate_gate(cfg, night, requests_today=0).reason == "open"


def test_a_restored_closed_window_still_closes_the_gate():
    cfg = ScheduleConfig(closed_hours=(0, 6))
    night = datetime(2026, 10, 14, 1, 0, tzinfo=timezone.utc)  # 03:00 Rome
    assert evaluate_gate(cfg, night, requests_today=0).reason == "closed_hours"


def test_a_new_rome_day_resets_only_the_counters():
    state = PaceState(WED_NOON, day=date(2026, 10, 13), requests_today=500, refusals_today=80,
                      description_run=2)
    roll_day(state, ScheduleConfig(), WED_NOON)
    assert (state.day, state.requests_today, state.refusals_today) == (date(2026, 10, 14), 0, 0)
    assert state.description_run == 2  # the turn share survives midnight
