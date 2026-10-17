from datetime import date, datetime, timedelta, timezone

import pytest

from src.linkedin.pace import (
    PaceState, base_spacing, evaluate_gate, must_yield, next_spacing, refusal_wait_seconds,
    roll_day, stored_spacing, update_nudge,
)
from src.linkedin.schedule import RefusalPolicy, ScheduleConfig, floor_spacing_seconds

WED_NOON = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)  # 12:00 Rome, Wednesday
SAT_NOON = datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc)
FRI_2359 = datetime(2026, 10, 16, 21, 59, tzinfo=timezone.utc)   # 23:59 Rome, Friday
SAT_0600 = datetime(2026, 10, 17, 4, 0, tzinfo=timezone.utc)     # 06:00 Rome, Saturday
NOW_TS = WED_NOON.timestamp()
MID = lambda a, b: (a + b) / 2  # noqa: E731 - no jitter


def test_the_base_spacing_starts_at_75s_weekday_and_110s_weekend():
    assert base_spacing(PaceState(WED_NOON), ScheduleConfig(), WED_NOON) == 75
    assert base_spacing(PaceState(SAT_NOON), ScheduleConfig(), SAT_NOON) == 110


def test_the_stored_spacing_of_each_window_is_clamped_to_its_floor_and_ceiling():
    cfg = ScheduleConfig()
    assert stored_spacing(PaceState(WED_NOON), cfg, "weekday") == 75
    assert stored_spacing(PaceState(WED_NOON), cfg, "weekend") == 110
    state = PaceState(WED_NOON, weekday_spacing=10.0, weekend_spacing=500.0)
    assert stored_spacing(state, cfg, "weekday") == pytest.approx(18 * 3600 / 1400)
    assert stored_spacing(state, cfg, "weekend") == 220
    assert base_spacing(state, cfg, SAT_NOON) == 220


def test_each_spacing_is_jittered_twenty_percent_and_never_below_the_floor():
    cfg, low, high = ScheduleConfig(), (lambda a, b: a), (lambda a, b: b)
    assert next_spacing(PaceState(WED_NOON), cfg, WED_NOON, low) == pytest.approx(60.0)
    assert next_spacing(PaceState(WED_NOON), cfg, WED_NOON, high) == pytest.approx(90.0)
    at_floor = PaceState(WED_NOON, weekday_spacing=floor_spacing_seconds(cfg, "weekday"))
    assert next_spacing(at_floor, cfg, WED_NOON, low) == pytest.approx(18 * 3600 / 1400)


def test_the_hour_nudge_multiplies_the_spacing():
    assert next_spacing(PaceState(WED_NOON, nudge=1.5), ScheduleConfig(), WED_NOON, MID) == pytest.approx(112.5)


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


def test_more_than_ten_percent_refused_in_the_last_hour_slows_the_spacing_by_half():
    state = PaceState(WED_NOON, request_times=[WED_NOON - timedelta(minutes=m) for m in range(20)],
                      refusal_times=[WED_NOON - timedelta(minutes=m) for m in (1, 5, 9)])
    update_nudge(state, WED_NOON, ScheduleConfig())
    assert state.nudge == 1.5 and state.nudged_at == WED_NOON


def test_exactly_ten_percent_does_not_nudge():
    state = PaceState(WED_NOON, request_times=[WED_NOON - timedelta(minutes=m) for m in range(20)],
                      refusal_times=[WED_NOON - timedelta(minutes=m) for m in (1, 5)])
    update_nudge(state, WED_NOON, ScheduleConfig())
    assert state.nudge == 1.0 and state.nudged_at is None


def test_the_nudge_rises_at_most_once_an_hour_and_stops_at_three():
    state, seen = PaceState(WED_NOON), []
    for minutes in (0, 30, 60, 120, 180):
        now = WED_NOON + timedelta(minutes=minutes)
        state.request_times = [now - timedelta(minutes=m) for m in range(20)]
        state.refusal_times = [now - timedelta(minutes=m) for m in (1, 5, 9)]
        update_nudge(state, now, ScheduleConfig())
        seen.append(state.nudge)
    assert seen == [1.5, 1.5, 2.25, 3.0, 3.0]


def test_an_hour_without_refusals_returns_the_nudge_to_base():
    state = PaceState(WED_NOON, nudge=2.25, nudged_at=WED_NOON - timedelta(minutes=90),
                      refusal_times=[WED_NOON - timedelta(minutes=61)])
    update_nudge(state, WED_NOON, ScheduleConfig())
    assert state.nudge == 1.0 and state.refusal_times == []


def test_a_fresh_burst_after_the_reset_nudges_without_waiting_out_the_old_hour():
    # The hour_min_requests guard is off here: this test is about the nudged_at reset, so
    # the small burst below is deliberately judged.
    schedule = ScheduleConfig(refusals=RefusalPolicy(hour_min_requests=1))
    first = WED_NOON - timedelta(minutes=70)  # one refusal, then the nudge 55 min later
    state = PaceState(first, request_times=[first + timedelta(minutes=m) for m in range(0, 55, 10)],
                      refusal_times=[first])
    update_nudge(state, first + timedelta(minutes=55), schedule)
    assert state.nudge == 1.5
    update_nudge(state, first + timedelta(minutes=61), schedule)
    assert state.nudge == 1.0
    state.request_times = [WED_NOON - timedelta(minutes=m) for m in range(4)]
    state.refusal_times = [WED_NOON - timedelta(minutes=m) for m in (0, 1, 2)]
    update_nudge(state, WED_NOON, schedule)
    assert state.nudge == 1.5 and state.nudged_at == WED_NOON


def test_the_morning_after_the_night_pause_starts_from_base_and_does_not_jump_to_the_cap():
    # The 2026-10-17 06:00 case: x2.25 at 23:59, the night pause, then the very first request
    # of the morning is refused. The old code read 1 refusal in 1 request as a 100 % hour and
    # pinned the nudge at its 3x cap for the rest of the day.
    state = PaceState(FRI_2359, nudge=2.25, nudged_at=FRI_2359,
                      request_times=[FRI_2359 - timedelta(minutes=m) for m in range(20)],
                      refusal_times=[FRI_2359 - timedelta(minutes=m) for m in (1, 5, 9)])
    state.request_times.append(SAT_0600)
    state.refusal_times.append(SAT_0600)
    update_nudge(state, SAT_0600, ScheduleConfig())
    assert state.nudge == 1.0
    assert state.nudged_at is None
    assert state.request_times == [SAT_0600] and state.refusal_times == [SAT_0600]


def test_the_hour_nudge_needs_the_minimum_sample_before_it_can_rise():
    state = PaceState(WED_NOON, request_times=[WED_NOON - timedelta(minutes=m) for m in range(9)],
                      refusal_times=[WED_NOON - timedelta(minutes=m) for m in (0, 1, 2)])
    update_nudge(state, WED_NOON, ScheduleConfig())
    assert state.nudge == 1.0 and state.nudged_at is None
    state.request_times = [WED_NOON - timedelta(minutes=m) for m in range(10)]
    update_nudge(state, WED_NOON, ScheduleConfig())
    assert state.nudge == 1.5 and state.nudged_at == WED_NOON


def test_a_calm_hour_steps_the_nudge_back_down_at_most_once_an_hour():
    state = PaceState(WED_NOON, nudge=2.25, nudged_at=WED_NOON - timedelta(minutes=90),
                      request_times=[WED_NOON - timedelta(minutes=m) for m in range(12)],
                      refusal_times=[WED_NOON - timedelta(minutes=2)])
    update_nudge(state, WED_NOON, ScheduleConfig())
    assert state.nudge == 1.5 and state.nudged_at == WED_NOON
    update_nudge(state, WED_NOON + timedelta(minutes=30), ScheduleConfig())
    assert state.nudge == 1.5


def test_the_decay_never_goes_below_base():
    state = PaceState(WED_NOON, nudge=1.5, nudged_at=WED_NOON - timedelta(minutes=90),
                      request_times=[WED_NOON - timedelta(minutes=m) for m in range(12)],
                      refusal_times=[WED_NOON - timedelta(minutes=2)])
    update_nudge(state, WED_NOON, ScheduleConfig())
    assert state.nudge == 1.0


def _day(state, day, requests, refused, calm_days=None):
    state.day, state.requests_today, state.refusals_today = day, requests, refused
    if calm_days is not None:
        state.calm_days = calm_days
    return state


def test_three_calm_days_tighten_both_bases_ten_percent():
    cfg, state = ScheduleConfig(), PaceState(WED_NOON)
    for offset in range(3):
        _day(state, date(2026, 10, 12 + offset), 500, 5)
        roll_day(state, cfg, WED_NOON + timedelta(days=offset + 1))
    assert state.weekday_spacing == pytest.approx(67.5) and state.weekend_spacing == pytest.approx(99.0)
    assert state.calm_days == 0


def test_tightening_stops_at_the_cap_floor():
    cfg = ScheduleConfig()
    state = _day(PaceState(WED_NOON, weekday_spacing=48.0), date(2026, 10, 13), 500, 0, calm_days=2)
    roll_day(state, cfg, WED_NOON)
    assert state.weekday_spacing == pytest.approx(18 * 3600 / 1400)  # 43.2 would be under the floor
    assert state.weekend_spacing == pytest.approx(99.0)


def test_a_day_over_ten_percent_loosens_ten_percent_and_resets_the_calm_count():
    state = _day(PaceState(WED_NOON), date(2026, 10, 13), 200, 25, calm_days=2)
    roll_day(state, ScheduleConfig(), WED_NOON)
    assert state.weekday_spacing == pytest.approx(82.5) and state.weekend_spacing == pytest.approx(121.0)
    assert state.calm_days == 0


def test_loosening_stops_at_twice_the_start():
    state = _day(PaceState(WED_NOON, weekday_spacing=150.0), date(2026, 10, 13), 200, 25)
    roll_day(state, ScheduleConfig(), WED_NOON)
    assert state.weekday_spacing == pytest.approx(150.0)


def test_a_middling_day_breaks_the_calm_streak():
    state = _day(PaceState(WED_NOON), date(2026, 10, 13), 200, 10, calm_days=2)  # 5%
    roll_day(state, ScheduleConfig(), WED_NOON)
    assert (state.calm_days, state.weekday_spacing, state.weekend_spacing) == (0, None, None)


def test_a_quiet_day_gives_no_verdict():
    state = _day(PaceState(WED_NOON), date(2026, 10, 13), 50, 0, calm_days=2)
    roll_day(state, ScheduleConfig(), WED_NOON)
    assert (state.calm_days, state.weekday_spacing, state.weekend_spacing) == (2, None, None)
    assert (state.day, state.requests_today, state.refusals_today) == (date(2026, 10, 14), 0, 0)


def test_a_multi_day_gap_judges_only_the_stored_day_once():
    cfg = ScheduleConfig()
    state = _day(PaceState(WED_NOON), date(2026, 10, 12), 500, 5, calm_days=2)  # Monday
    thursday = datetime(2026, 10, 15, 10, 0, tzinfo=timezone.utc)
    roll_day(state, cfg, thursday)
    assert state.weekday_spacing == pytest.approx(67.5) and state.calm_days == 0
    assert (state.day, state.requests_today, state.refusals_today) == (date(2026, 10, 15), 0, 0)
    roll_day(state, cfg, thursday + timedelta(hours=2))
    assert state.weekday_spacing == pytest.approx(67.5) and state.calm_days == 0


@pytest.mark.parametrize("kind, overdue, last_kind, markers, expected", [
    ("description", False, "description", {"search"}, True),   # a waiting search goes first
    ("description", False, "search", {"search"}, True),
    ("description", True, "search", {"search"}, False),        # overdue: every second turn
    ("description", True, "description", {"search"}, True),
    ("description", False, None, set(), False),
    ("search", False, "search", {"description"}, False),       # never yields to a plain description
    ("search", False, "search", {"description", "overdue"}, True),
    ("search", False, "description", {"description", "overdue"}, False),
])
def test_must_yield_puts_searches_first_with_the_overdue_alternation(kind, overdue, last_kind, markers, expected):
    state = PaceState(WED_NOON, last_kind=last_kind, wanting={m: NOW_TS - 5 for m in markers})
    assert must_yield(state, kind, overdue, NOW_TS) is expected


def test_a_stale_marker_is_ignored():
    state = PaceState(WED_NOON, last_kind="search", wanting={"search": NOW_TS - 31})
    assert must_yield(state, "description", False, NOW_TS) is False


def test_the_gate_has_only_the_night_window_and_the_daily_cap():
    cfg = ScheduleConfig()
    assert evaluate_gate(cfg, WED_NOON, requests_today=0).reason == "open"
    assert evaluate_gate(cfg, WED_NOON, requests_today=1400).reason == "daily_cap"
    night = datetime(2026, 10, 14, 1, 0, tzinfo=timezone.utc)  # 03:00 Rome
    assert evaluate_gate(cfg, night, requests_today=0).reason == "closed_hours"
