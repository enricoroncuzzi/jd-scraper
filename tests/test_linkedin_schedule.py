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

ADAPTIVE = {
    "enabled": False,
    "levels_seconds": [75, 50, 37.5, 30, 25, 18.75, 15, 10.7],
    "max_level": 8,
    "protect_window_requests": 20,
    "protect_min_requests": 10,
    "protect_drop_rate": 0.30,
    "protect_drop_two_rate": 0.50,
    "protect_drop_streak": 4,
    "climb_min_requests": 200,
    "climb_min_requests_above_3x": 600,
    "above_3x_climbs_per_day": 1,
    "rolling_24h_ceiling_requests": 8640,
    "demand_hold_above_3x_waiting": 250,
    "llm_above_4x_stop_share": 0.75,
    "above_3x_hold_hours": 24,
    "climb_max_upper_refusal_rate": 0.12,
    "climb_max_gap_seconds": 3600,
    "memory_first_hours": 2,
    "memory_max_hours": 24,
    "clean_day_min_requests": 100,
    "demand_min_waiting": 1000,
    "demand_ease_waiting": 250,
    "demand_ease_interval_seconds": 3600,
    "signal_refresh_seconds": 300,
    "llm_climb_stop_share": 0.8,
    "emergency_spacing_seconds": 150,
    "emergency_clear_requests": 20,
    "emergency_connection_errors": 3,
    "emergency_connection_window_seconds": 600,
    "pause_after_block_seconds": 3600,
}


def test_the_adaptive_block_ships_on_with_the_approved_defaults():
    shipped = json.loads(Path("config/schedule.json").read_text())["adaptive"]
    cfg = load_schedule_config()
    a = cfg.adaptive
    assert shipped["enabled"] is True and a.enabled is True
    assert a.levels_seconds == (75, 50, 37.5, 30, 25, 18.75, 15, 10.7)
    assert (a.max_level, a.climb_min_requests_above_3x) == (8, 600)
    assert (a.above_3x_climbs_per_day, a.rolling_24h_ceiling_requests) == (1, 8640)
    assert (a.demand_hold_above_3x_waiting, a.llm_above_4x_stop_share) == (250, 0.75)
    assert a.above_3x_hold_hours == 24
    assert (a.protect_window_requests, a.protect_min_requests) == (20, 10)
    assert (a.protect_drop_rate, a.protect_drop_two_rate, a.protect_drop_streak) == (0.30, 0.50, 4)
    assert (a.climb_min_requests, a.climb_max_upper_refusal_rate) == (200, 0.12)
    assert (a.memory_first_hours, a.memory_max_hours) == (2, 24)
    assert (a.demand_min_waiting, a.demand_ease_waiting) == (1000, 250)
    assert a.llm_climb_stop_share == 0.8
    assert (a.emergency_spacing_seconds, a.emergency_clear_requests) == (150, 20)
    assert (a.emergency_connection_errors, a.emergency_connection_window_seconds) == (3, 600)
    assert a.pause_after_block_seconds == 3600
    for key, value in shipped.items():
        expected = tuple(value) if key == "levels_seconds" else value
        assert expected == getattr(a, key), key


def test_the_ladder_length_comes_from_config(tmp_path):
    six = load_schedule_config(_write(tmp_path, {**DEFAULTS, "adaptive": {
        **ADAPTIVE, "levels_seconds": [75, 50, 37.5, 30, 25, 18.75], "max_level": 6}}))
    assert len(six.adaptive.levels_seconds) == 6 and six.adaptive.max_level == 6
    five = load_schedule_config(_write(tmp_path, {**DEFAULTS, "adaptive": {
        **ADAPTIVE, "levels_seconds": [75, 50, 37.5, 30, 25], "max_level": 5}}))
    assert len(five.adaptive.levels_seconds) == 5 and five.adaptive.max_level == 5


def test_a_ceiling_above_the_ladder_or_at_the_first_level_is_rejected(tmp_path):
    for value in (9, 0):
        with pytest.raises(ValueError, match="max_level"):
            load_schedule_config(_write(tmp_path, {**DEFAULTS, "adaptive": {
                **ADAPTIVE, "max_level": value}}))


def test_the_emergency_base_change_loads_while_the_ladder_is_off(tmp_path):
    cfg = load_schedule_config(_write(tmp_path, {**DEFAULTS, "pace": {
        **PACE, "start_spacing_seconds": 91}, "adaptive": {**ADAPTIVE, "enabled": False}}))
    assert start_spacing_seconds(cfg) == 91


def test_enabling_the_ladder_requires_the_first_level_to_match_the_base(tmp_path):
    with pytest.raises(ValueError, match="levels_seconds"):
        load_schedule_config(_write(tmp_path, {**DEFAULTS, "pace": {
            **PACE, "start_spacing_seconds": 91}, "adaptive": {**ADAPTIVE, "enabled": True}}))


@pytest.mark.parametrize("override", [
    # Four levels are legal now; this keeps raising only because the inherited
    # max_level 8 exceeds the ladder, which is the ceiling rule.
    {"levels_seconds": [75, 50, 37.5, 30]},
    {"levels_seconds": [75]},
    {"levels_seconds": [75, 60, 50, 40, 30, 25, 20, 15, 10]},
    {"max_level": 9},
    {"climb_min_requests_above_3x": 0},
    {"above_3x_climbs_per_day": 0},
    {"rolling_24h_ceiling_requests": 0},
    {"demand_hold_above_3x_waiting": 1001},
    {"llm_above_4x_stop_share": 0},
    {"above_3x_hold_hours": 0},
    {"levels_seconds": [75, 80, 37.5, 30, 25]},
    {"protect_min_requests": 0},
    {"protect_min_requests": 21},
    {"protect_drop_rate": 0.0},
    {"protect_drop_two_rate": 0.20},
    {"protect_drop_streak": 21},
    {"climb_max_upper_refusal_rate": 1.1},
    {"llm_climb_stop_share": 1.1},
    {"memory_max_hours": 1},
    {"demand_ease_waiting": 1001},
    {"emergency_spacing_seconds": 10.7},   # must exceed the last level, now 10.7
    {"emergency_connection_errors": 0},
    {"pause_after_block_seconds": 30},
    {"protect_drop_rte": 0.4},
])
def test_a_malformed_adaptive_block_raises(tmp_path, override):
    with pytest.raises(ValueError):
        load_schedule_config(_write(tmp_path, {**DEFAULTS, "adaptive": {**ADAPTIVE, **override}}))
