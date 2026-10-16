import json
from datetime import datetime, timedelta, timezone

import pytest

from src.linkedin.schedule import (
    ScheduleConfig, daily_cap, floor_spacing_seconds, in_closed_window, is_weekend,
    load_schedule_config, local_day, open_hours_per_day, open_seconds_between, requests_today_for,
    start_spacing_seconds, window_name,
)

# 2026-10-14 is a Wednesday; the 17th and 18th are Saturday and Sunday.
WED = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
SAT = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
SUN = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def test_the_shipped_schedule_loads():
    cfg = load_schedule_config()
    assert cfg.timezone == "Europe/Rome"
    assert cfg.closed_hours == (0, 6)
    assert (cfg.weekday_cap, cfg.weekend_cap) == (1400, 800)
    assert (cfg.breaker_refusals, cfg.breaker_window_minutes) == (3, 30)
    assert cfg.rest_ladder_hours == (2, 6, 24)
    assert cfg.weekend_taper_min_interval_hours == 24


def test_the_shipped_schedule_carries_the_pace_and_refusal_numbers():
    cfg = load_schedule_config()
    p, r = cfg.pace, cfg.refusals
    assert (p.start_weekday_seconds, p.start_weekend_seconds, p.jitter) == (75, 110, 0.2)
    assert (p.calm_days_to_tighten, p.calm_refusal_rate, p.loosen_refusal_rate, p.step) == (3, 0.02, 0.10, 0.10)
    assert (p.min_day_requests, p.max_spacing_factor) == (100, 2.0)
    assert (r.first_wait_seconds, r.max_wait_seconds) == (60, 1800)
    assert (r.hour_refusal_rate, r.hour_multiplier, r.hour_max_multiplier) == (0.10, 1.5, 3.0)


def test_the_floor_is_the_open_hours_over_the_cap():
    cfg = load_schedule_config()
    assert open_hours_per_day(cfg) == 18
    assert floor_spacing_seconds(cfg, "weekday") == pytest.approx(18 * 3600 / 1400)  # 46.29 s
    assert floor_spacing_seconds(cfg, "weekend") == pytest.approx(81.0)


def test_open_time_skips_the_quiet_window():
    cfg = ScheduleConfig()
    # Wed 22:00 Rome (CEST, UTC+2) to Thu 08:00 Rome: 2 h before midnight + 2 h after 06:00
    start = datetime(2026, 10, 14, 20, 0, tzinfo=timezone.utc)
    assert open_seconds_between(cfg, start, start + timedelta(hours=10)) == 4 * 3600
    assert open_seconds_between(cfg, start, start) == 0


def test_open_time_is_dst_correct():
    cfg = ScheduleConfig()
    # Sat 24 Oct 06:00 Rome (UTC+2) to Mon 26 Oct 06:00 Rome (UTC+1): the extra hour is at 03:00, inside the window
    start = datetime(2026, 10, 24, 4, 0, tzinfo=timezone.utc)
    end = datetime(2026, 10, 26, 5, 0, tzinfo=timezone.utc)
    assert open_seconds_between(cfg, start, end) == 36 * 3600


def test_start_spacing_seconds_returns_the_window_start():
    cfg = ScheduleConfig()
    assert start_spacing_seconds(cfg, "weekday") == 75 and start_spacing_seconds(cfg, "weekend") == 110


def test_the_weekday_and_the_weekend_get_their_own_caps():
    cfg = ScheduleConfig()
    assert not is_weekend(cfg, WED) and window_name(cfg, WED) == "weekday"
    assert daily_cap(cfg, WED) == 1400
    assert is_weekend(cfg, SAT) and is_weekend(cfg, SUN)
    assert window_name(cfg, SAT) == "weekend"
    assert daily_cap(cfg, SAT) == 800
    assert daily_cap(cfg, SUN) == 800


def test_the_quiet_window_is_rome_0000_to_0600():
    cfg = ScheduleConfig()
    # 2026-10-14 00:30 Rome = 2026-10-13 22:30 UTC (CEST, UTC+2).
    assert in_closed_window(cfg, datetime(2026, 10, 13, 22, 30, tzinfo=timezone.utc))
    assert in_closed_window(cfg, datetime(2026, 10, 14, 3, 59, tzinfo=timezone.utc))
    assert not in_closed_window(cfg, datetime(2026, 10, 14, 4, 0, tzinfo=timezone.utc))  # 06:00 Rome
    assert not in_closed_window(cfg, WED)


@pytest.mark.parametrize("utc, closed", [
    # Fall back on 2026-10-25: 22:00 UTC is 00:00 CET, 05:00 UTC is 06:00 CET.
    (datetime(2026, 10, 24, 21, 59, tzinfo=timezone.utc), False),
    (datetime(2026, 10, 24, 22, 0, tzinfo=timezone.utc), True),
    (datetime(2026, 10, 25, 4, 59, tzinfo=timezone.utc), True),
    (datetime(2026, 10, 25, 5, 0, tzinfo=timezone.utc), False),
    # Spring forward on 2026-03-29: 23:00 UTC is 00:00 CEST, 04:00 UTC is 06:00 CEST.
    (datetime(2026, 3, 28, 22, 59, tzinfo=timezone.utc), False),
    (datetime(2026, 3, 28, 23, 0, tzinfo=timezone.utc), True),
    (datetime(2026, 3, 29, 3, 59, tzinfo=timezone.utc), True),
    (datetime(2026, 3, 29, 4, 0, tzinfo=timezone.utc), False),
])
def test_the_quiet_window_is_dst_correct(utc, closed):
    assert in_closed_window(ScheduleConfig(), utc) is closed


def test_the_budget_day_is_dst_correct():
    cfg = ScheduleConfig()
    assert local_day(cfg, datetime(2026, 10, 24, 22, 0, tzinfo=timezone.utc)).isoformat() == "2026-10-25"
    assert local_day(cfg, datetime(2026, 3, 28, 23, 0, tzinfo=timezone.utc)).isoformat() == "2026-03-29"


def test_the_budget_belongs_to_the_rome_calendar_day():
    cfg = ScheduleConfig()
    assert local_day(cfg, WED).isoformat() == "2026-10-14"
    # 23:30 UTC on the 14th is 01:30 Rome on the 15th.
    assert local_day(cfg, datetime(2026, 10, 14, 23, 30, tzinfo=timezone.utc)).isoformat() == "2026-10-15"


def test_a_stored_counter_only_counts_when_its_day_is_today():
    cfg = ScheduleConfig()
    assert requests_today_for(WED.date(), 7, cfg, WED) == 7
    assert requests_today_for(WED.date(), 7, cfg, WED.replace(day=15)) == 0
    assert requests_today_for(None, 7, cfg, WED) == 0


def _write(tmp_path, data) -> str:
    path = tmp_path / "schedule.json"
    path.write_text(json.dumps(data))
    return str(path)


DEFAULTS = {
    "timezone": "Europe/Rome",
    "closed_hours": [0, 6],
    "daily_caps": {"weekday": 1400, "weekend": 800},
    "breaker": {"refusals": 3, "window_minutes": 30, "rest_ladder_hours": [2, 6, 24]},
    "weekend_taper_min_interval_hours": 24,
    "pace": {
        "start_spacing_seconds": {"weekday": 75, "weekend": 110},
        "jitter": 0.2,
        "max_spacing_factor": 2.0,
        "tighten": {"calm_days": 3, "calm_refusal_rate": 0.02, "loosen_refusal_rate": 0.10,
                    "step": 0.10, "min_day_requests": 100},
    },
    "refusals": {
        "first_wait_seconds": 60,
        "max_wait_seconds": 1800,
        "hour_refusal_rate": 0.10,
        "hour_multiplier": 1.5,
        "hour_max_multiplier": 3.0,
    },
}
PACE, TIGHTEN, REFUSALS = DEFAULTS["pace"], DEFAULTS["pace"]["tighten"], DEFAULTS["refusals"]


@pytest.mark.parametrize("override", [
    {"timezone": "Mars/Olympus"},
    {"timezone": ""},
    {"closed_hours": [6, 0]},
    {"closed_hours": [0]},
    {"closed_hours": [0, 25]},
    {"closed_hours": [0, 24]},
    {"daily_caps": {"weekday": 0, "weekend": 800}},
    {"daily_caps": {"weekday": 1400}},
    {"breaker": {"refusals": 0, "window_minutes": 30, "rest_ladder_hours": [2]}},
    {"breaker": {"refusals": 3, "window_minutes": 0, "rest_ladder_hours": [2]}},
    {"breaker": {"refusals": 3, "window_minutes": 30, "rest_ladder_hours": []}},
    {"weekend_taper_min_interval_hours": 0},
    {"pace": {**PACE, "jitter": 0.6}},
    {"pace": {**PACE, "tighten": {**TIGHTEN, "step": 0}}},
    {"pace": {**PACE, "tighten": {**TIGHTEN, "calm_refusal_rate": 1.5}}},
    {"pace": {**PACE, "start_spacing_seconds": {"weekday": 0, "weekend": 110}}},
    {"refusals": {**REFUSALS, "max_wait_seconds": 30}},
    {"refusals": {**REFUSALS, "hour_multiplier": 0.9}},
    {"pace": {**PACE, "max_spacing_factor": 0.5}},
])
def test_a_malformed_schedule_raises(tmp_path, override):
    with pytest.raises(ValueError):
        load_schedule_config(_write(tmp_path, {**DEFAULTS, **override}))
