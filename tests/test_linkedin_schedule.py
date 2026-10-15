import json
from datetime import datetime, timezone

import pytest

from src.linkedin.schedule import (
    ScheduleConfig, daily_cap, in_closed_window, is_weekend, load_schedule_config, local_day,
    requests_today_for, window_name,
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
}


@pytest.mark.parametrize("override", [
    {"timezone": "Mars/Olympus"},
    {"timezone": ""},
    {"closed_hours": [6, 0]},
    {"closed_hours": [0]},
    {"closed_hours": [0, 25]},
    {"daily_caps": {"weekday": 0, "weekend": 800}},
    {"daily_caps": {"weekday": 1400}},
    {"breaker": {"refusals": 0, "window_minutes": 30, "rest_ladder_hours": [2]}},
    {"breaker": {"refusals": 3, "window_minutes": 0, "rest_ladder_hours": [2]}},
    {"breaker": {"refusals": 3, "window_minutes": 30, "rest_ladder_hours": []}},
    {"weekend_taper_min_interval_hours": 0},
])
def test_a_malformed_schedule_raises(tmp_path, override):
    with pytest.raises(ValueError):
        load_schedule_config(_write(tmp_path, {**DEFAULTS, **override}))
