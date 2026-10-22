import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.linkedin.schedule import (
    ScheduleConfig, in_closed_window, load_schedule_config, local_day,
    open_seconds_between, requests_today_for, start_spacing_seconds,
)

# 2026-10-14 is a Wednesday; the 17th and 18th are Saturday and Sunday.
WED = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
SAT = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
SUN = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def test_the_shipped_schedule_loads():
    cfg = load_schedule_config()
    assert cfg.timezone == "Europe/Rome"
    assert cfg.closed_hours is None


def test_the_shipped_schedule_carries_no_request_cap():
    cfg = load_schedule_config()
    assert not hasattr(cfg, "weekday_cap") and not hasattr(cfg, "weekend_cap")
    # The shipped config file itself carries no cap either.
    shipped = json.loads(Path("config/schedule.json").read_text())
    assert "daily_caps" not in shipped


def test_a_leftover_daily_caps_block_is_ignored(tmp_path):
    # A stale or rolled-back schedule.json may still carry the old caps; loading it must
    # never stop the service.
    cfg = load_schedule_config(_write(tmp_path, {**DEFAULTS,
                                                 "daily_caps": {"weekday": 1400, "weekend": 800}}))
    assert cfg.timezone == "Europe/Rome" and cfg.closed_hours is None
    assert not hasattr(cfg, "weekday_cap") and not hasattr(cfg, "weekend_cap")


def test_a_leftover_daily_caps_block_is_ignored_even_when_it_is_junk(tmp_path):
    for leftover in ({}, "off", {"weekday": 0}, {"weekday": 1400}):
        cfg = load_schedule_config(_write(tmp_path, {**DEFAULTS, "daily_caps": leftover}))
        assert cfg.pace.start_spacing_seconds == 75


def test_the_shipped_schedule_has_no_closed_window():
    cfg = load_schedule_config()
    assert not in_closed_window(cfg, datetime(2026, 10, 13, 22, 30, tzinfo=timezone.utc))  # Rome 00:30


def test_a_leftover_breaker_block_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="removed"):
        load_schedule_config(_write(tmp_path, {**DEFAULTS,
                                               "breaker": {"refusals": 3, "window_minutes": 30}}))


def test_a_leftover_taper_key_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="removed"):
        load_schedule_config(_write(tmp_path, {**DEFAULTS, "weekend_taper_min_interval_hours": 24}))


def test_the_shipped_schedule_carries_the_pace_and_refusal_numbers():
    cfg = load_schedule_config()
    p, r = cfg.pace, cfg.refusals
    assert (p.start_spacing_seconds, p.jitter) == (75, 0.2)
    assert p.description_turn_share == pytest.approx(2 / 3, abs=1e-3)
    assert (r.first_wait_seconds, r.max_wait_seconds) == (60, 1800)


def test_a_leftover_hour_nudge_key_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="removed"):
        load_schedule_config(_write(tmp_path, {**DEFAULTS,
                                               "refusals": {**REFUSALS, "hour_multiplier": 1.5}}))


def test_a_leftover_day_verdict_key_is_rejected(tmp_path):
    for override in ({"pace": {**PACE, "tighten": TIGHTEN}},
                     {"pace": {**PACE, "max_spacing_factor": 2.0}}):
        with pytest.raises(ValueError, match="removed"):
            load_schedule_config(_write(tmp_path, {**DEFAULTS, **override}))


def test_open_time_skips_the_quiet_window():
    cfg = ScheduleConfig(closed_hours=(0, 6))
    # Wed 22:00 Rome (CEST, UTC+2) to Thu 08:00 Rome: 2 h before midnight + 2 h after 06:00
    start = datetime(2026, 10, 14, 20, 0, tzinfo=timezone.utc)
    assert open_seconds_between(cfg, start, start + timedelta(hours=10)) == 4 * 3600
    assert open_seconds_between(cfg, start, start) == 0


def test_without_a_closed_window_open_time_is_wall_time():
    cfg = ScheduleConfig()
    start = datetime(2026, 10, 14, 20, 0, tzinfo=timezone.utc)
    assert open_seconds_between(cfg, start, start + timedelta(hours=10)) == 10 * 3600
    assert open_seconds_between(cfg, start, start) == 0


def test_open_time_is_dst_correct():
    cfg = ScheduleConfig(closed_hours=(0, 6))
    # Sat 24 Oct 06:00 Rome (UTC+2) to Mon 26 Oct 06:00 Rome (UTC+1): the extra hour is at 03:00, inside the window
    start = datetime(2026, 10, 24, 4, 0, tzinfo=timezone.utc)
    end = datetime(2026, 10, 26, 5, 0, tzinfo=timezone.utc)
    assert open_seconds_between(cfg, start, end) == 36 * 3600


def test_open_time_without_a_window_is_dst_correct():
    cfg = ScheduleConfig()
    # The same 49 h of wall time, including the extra hour at 03:00 Rome.
    start = datetime(2026, 10, 24, 4, 0, tzinfo=timezone.utc)
    end = datetime(2026, 10, 26, 5, 0, tzinfo=timezone.utc)
    assert open_seconds_between(cfg, start, end) == 49 * 3600


def test_start_spacing_seconds_returns_the_one_base_spacing():
    cfg = ScheduleConfig()
    assert start_spacing_seconds(cfg) == 75
    assert not hasattr(cfg.pace, "start_weekday_seconds")
    assert not hasattr(cfg.pace, "start_weekend_seconds")


def test_the_shipped_schedule_has_one_base_spacing_for_every_day():
    cfg = load_schedule_config()
    shipped = json.loads(Path("config/schedule.json").read_text())
    assert start_spacing_seconds(cfg) == 75
    assert shipped["pace"]["start_spacing_seconds"] == 75
    assert "weekday" not in shipped["pace"] and "weekend" not in shipped["pace"]


def test_a_legacy_spacing_object_still_loads_and_uses_weekday(tmp_path, capsys):
    cfg = load_schedule_config(_write(tmp_path, {**DEFAULTS, "pace": {
        **PACE, "start_spacing_seconds": {"weekday": 91, "weekend": 133}}}))
    assert start_spacing_seconds(cfg) == 91
    assert "weekday" in capsys.readouterr().out


def test_the_quiet_window_is_rome_0000_to_0600():
    cfg = ScheduleConfig(closed_hours=(0, 6))
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
    assert in_closed_window(ScheduleConfig(closed_hours=(0, 6)), utc) is closed


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
    "closed_hours": None,
    "pace": {
        "start_spacing_seconds": 75,
        "jitter": 0.2,
        "description_turn_share": 2 / 3,
    },
    "refusals": {
        "first_wait_seconds": 60,
        "max_wait_seconds": 1800,
    },
}
PACE, REFUSALS = DEFAULTS["pace"], DEFAULTS["refusals"]
TIGHTEN = {"calm_days": 3, "calm_refusal_rate": 0.02, "loosen_refusal_rate": 0.10,
           "step": 0.10, "min_day_requests": 100}


@pytest.mark.parametrize("override", [
    {"timezone": "Mars/Olympus"},
    {"timezone": ""},
    {"closed_hours": [6, 0]},
    {"closed_hours": [0]},
    {"closed_hours": [0, 25]},
    {"closed_hours": [0, 24]},
    {"closed_hours": "off"},
    {"closed_hours": {}},
    {"pace": {**PACE, "jitter": 0.6}},
    {"pace": {**PACE, "description_turn_share": 1.0}},
    {"pace": {**PACE, "description_turn_share": 0.4}},
    {"pace": {**PACE, "start_spacing_seconds": 0}},
    {"pace": {**PACE, "start_spacing_seconds": {"weekday": 0, "weekend": 110}}},
    {"refusals": {**REFUSALS, "max_wait_seconds": 30}},
    {"refusals": {**REFUSALS, "hour_min_requests": 0}},
])
def test_a_malformed_schedule_raises(tmp_path, override):
    with pytest.raises(ValueError):
        load_schedule_config(_write(tmp_path, {**DEFAULTS, **override}))


def test_a_null_closed_window_is_accepted(tmp_path):
    cfg = load_schedule_config(_write(tmp_path, {**DEFAULTS, "closed_hours": None}))
    assert cfg.closed_hours is None


def test_a_configured_closed_window_is_still_accepted(tmp_path):
    cfg = load_schedule_config(_write(tmp_path, {**DEFAULTS, "closed_hours": [0, 6]}))
    assert cfg.closed_hours == (0, 6)


def test_a_missing_closed_hours_key_is_rejected(tmp_path):
    data = {key: value for key, value in DEFAULTS.items() if key != "closed_hours"}
    with pytest.raises(ValueError, match="closed_hours"):
        load_schedule_config(_write(tmp_path, data))
