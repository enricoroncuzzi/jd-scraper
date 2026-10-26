"""The LinkedIn schedule: the optional closed window and the steady-pace policy, loaded
from config/schedule.json (spec section 4).

The collector and the processor share one PaceBudget on host `linkedin.com`, so
this module holds only the policy: what the Rome clock and the per-day counters
mean, and the numbers the pace is built from. There is one base spacing for every
day; there is no daily request cap, and the counters are operator information only.
Enforcement lives in src/linkedin/pace.py, the single choke point every LinkedIn
request passes through.
"""
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from src.jobstore.db import utcnow

SCHEDULE_CONFIG_PATH = "config/schedule.json"
HOURS_PER_DAY = 24
# The ladder's hard bound: state normalization and the per-level memory can hold this
# many levels, so a config that lists more is rejected at start rather than silently
# capped later. src/linkedin/adaptive.py imports it as MAX_LEVELS.
MAX_LADDER_LEVELS = 8


@dataclass(frozen=True)
class PacePolicy:
    """The fixed pace (2026-10-18 refusal study): one constant base spacing on every
    day, jittered every turn, with a share of the turns reserved for descriptions
    when a search is also waiting."""

    start_spacing_seconds: float = 75.0
    jitter: float = 0.2
    description_turn_share: float = 2.0 / 3.0


@dataclass(frozen=True)
class AdaptivePolicy:
    """The self-learning pace ladder (config/schedule.json `adaptive`). Every threshold
    is a config value; the shipped block enables it, and `enabled: false` is the off switch."""

    enabled: bool = False
    levels_seconds: tuple[float, ...] = (75.0, 50.0, 37.5, 30.0, 25.0, 18.75, 15.0, 10.7)
    max_level: int = MAX_LADDER_LEVELS
    protect_window_requests: int = 20
    protect_min_requests: int = 10
    protect_drop_rate: float = 0.30
    protect_drop_two_rate: float = 0.50
    protect_drop_streak: int = 4
    climb_min_requests: int = 200
    climb_min_requests_above_3x: int = 600
    above_3x_climbs_per_day: int = 1
    rolling_24h_ceiling_requests: int = 5000
    demand_hold_above_3x_waiting: int = 250
    llm_above_4x_stop_share: float = 0.75
    above_3x_hold_hours: int = 24
    climb_max_upper_refusal_rate: float = 0.12
    climb_max_gap_seconds: int = 3600
    memory_first_hours: int = 2
    memory_max_hours: int = 24
    clean_day_min_requests: int = 100
    demand_min_waiting: int = 1000
    demand_ease_waiting: int = 250
    demand_ease_interval_seconds: int = 3600
    signal_refresh_seconds: int = 300
    llm_climb_stop_share: float = 0.8
    emergency_spacing_seconds: float = 150.0
    emergency_clear_requests: int = 20
    emergency_connection_errors: int = 3
    emergency_connection_window_seconds: int = 600
    pause_after_block_seconds: int = 3600


@dataclass(frozen=True)
class RefusalPolicy:
    """What a refusal costs (design section 4.2): a capped, doubling wait. There is
    no automatic slowing: the pace is the same before and after a refusal spell."""

    first_wait_seconds: float = 60.0
    max_wait_seconds: float = 1800.0


@dataclass(frozen=True)
class ScheduleConfig:
    timezone: str = "Europe/Rome"
    closed_hours: tuple[int, int] | None = None
    pace: PacePolicy = PacePolicy()
    refusals: RefusalPolicy = RefusalPolicy()
    adaptive: AdaptivePolicy = AdaptivePolicy()

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def _require_number(data: dict, key: str, where: str, *, minimum: float = 0.0,
                    exclusive: bool = False) -> float:
    value = data.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{where}.{key} must be a number, got {value!r}")
    if (exclusive and value <= minimum) or (not exclusive and value < minimum):
        bound = f"> {minimum}" if exclusive else f">= {minimum}"
        raise ValueError(f"{where}.{key} must be {bound}, got {value!r}")
    return float(value)


def _require_ratio(data: dict, key: str, where: str, *, minimum: float = 0.0,
                   maximum: float = 1.0, exclusive_min: bool = False,
                   exclusive_max: bool = False) -> float:
    value = _require_number(data, key, where, minimum=minimum, exclusive=exclusive_min)
    if (exclusive_max and value >= maximum) or (not exclusive_max and value > maximum):
        raise ValueError(f"{where}.{key} must be <= {maximum}, got {value!r}")
    return value


def _require_absent(data: dict, keys: tuple[str, ...], where: str, why: str) -> None:
    for key in keys:
        if key in data:
            raise ValueError(f"{where}.{key} was removed ({why}); delete it")


def _load_pace(data: dict) -> PacePolicy:
    pace = data.get("pace")
    if not isinstance(pace, dict):
        raise ValueError("schedule.pace must be an object")
    _require_absent(pace, ("max_spacing_factor", "tighten"), "schedule.pace",
                    "the pace is fixed, never widened or tightened")
    starts = pace.get("start_spacing_seconds")
    if isinstance(starts, dict):
        # A stale or rolled-back config may still carry the old object form. Use the
        # weekday value and warn once; never fail the service over it.
        base = _require_number(starts, "weekday", "schedule.pace.start_spacing_seconds",
                               exclusive=True)
        print("[schedule] pace.start_spacing_seconds.weekday/.weekend are gone; "
              f"using weekday ({base:g} s) until the config is updated")
    else:
        base = _require_number(pace, "start_spacing_seconds", "schedule.pace", exclusive=True)
    jitter = _require_ratio(pace, "jitter", "schedule.pace", maximum=0.5, exclusive_max=True)
    share = _require_ratio(pace, "description_turn_share", "schedule.pace", minimum=0.5,
                           maximum=1.0, exclusive_max=True)
    return PacePolicy(
        start_spacing_seconds=base,
        jitter=jitter,
        description_turn_share=share,
    )


def _load_refusals(data: dict) -> RefusalPolicy:
    refusals = data.get("refusals")
    if not isinstance(refusals, dict):
        raise ValueError("schedule.refusals must be an object")
    _require_absent(refusals, ("hour_refusal_rate", "hour_multiplier", "hour_max_multiplier",
                               "hour_min_requests"), "schedule.refusals",
                    "the hour nudge was removed, the pace is fixed")
    first = _require_number(refusals, "first_wait_seconds", "schedule.refusals", exclusive=True)
    maximum = _require_number(refusals, "max_wait_seconds", "schedule.refusals", exclusive=True)
    if first > maximum:
        raise ValueError("schedule.refusals.max_wait_seconds must be at least first_wait_seconds")
    return RefusalPolicy(first, maximum)


ADAPTIVE_KEYS = {
    "enabled", "levels_seconds", "max_level", "protect_window_requests", "protect_min_requests",
    "protect_drop_rate", "protect_drop_two_rate", "protect_drop_streak",
    "climb_min_requests", "climb_min_requests_above_3x", "above_3x_climbs_per_day",
    "rolling_24h_ceiling_requests", "demand_hold_above_3x_waiting",
    "llm_above_4x_stop_share", "above_3x_hold_hours",
    "climb_max_upper_refusal_rate", "climb_max_gap_seconds",
    "memory_first_hours", "memory_max_hours", "clean_day_min_requests",
    "demand_min_waiting", "demand_ease_waiting", "demand_ease_interval_seconds",
    "signal_refresh_seconds", "llm_climb_stop_share", "emergency_spacing_seconds",
    "emergency_clear_requests", "emergency_connection_errors",
    "emergency_connection_window_seconds", "pause_after_block_seconds",
}


def _adaptive_int(data: dict, key: str, default: int, *, minimum: int = 1) -> int:
    value = data.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"schedule.adaptive.{key} must be an integer >= {minimum}, got {value!r}")
    return value


def _adaptive_ratio(data: dict, key: str, default: float) -> float:
    value = data.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 < value <= 1:
        raise ValueError(f"schedule.adaptive.{key} must be a number in (0, 1], got {value!r}")
    return float(value)


def _load_adaptive(data: dict, base_spacing: float) -> AdaptivePolicy:
    """Read and validate config/schedule.json `adaptive`. A missing block falls back to the
    code default, disabled; an unknown key is a typo that must not silently keep a
    safety default."""
    adaptive = data.get("adaptive")
    if adaptive is None:
        return AdaptivePolicy()
    if not isinstance(adaptive, dict):
        raise ValueError("schedule.adaptive must be an object")
    unknown = set(adaptive) - ADAPTIVE_KEYS
    if unknown:
        raise ValueError(f"schedule.adaptive has unknown keys: {sorted(unknown)}")
    enabled = adaptive.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(f"schedule.adaptive.enabled must be a boolean, got {enabled!r}")
    # A config without the key keeps the pre-ladder five levels, so an old file runs at
    # the ceiling it always had (max_level defaults to the ladder length).
    levels = adaptive.get("levels_seconds", [75, 50, 37.5, 30, 25])
    if (not isinstance(levels, list) or len(levels) < 2
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
                       for v in levels)
            or any(a <= b for a, b in zip(levels, levels[1:]))):
        raise ValueError("schedule.adaptive.levels_seconds must be 2 or more strictly "
                         f"decreasing positive numbers, got {levels!r}")
    if len(levels) > MAX_LADDER_LEVELS:
        raise ValueError("schedule.adaptive.levels_seconds must not exceed "
                         f"MAX_LADDER_LEVELS ({MAX_LADDER_LEVELS}), got {len(levels)}")
    levels = tuple(float(v) for v in levels)
    max_level = _adaptive_int(adaptive, "max_level", len(levels), minimum=2)
    if max_level > len(levels):
        raise ValueError("schedule.adaptive.max_level must be <= the number of "
                         f"levels_seconds ({len(levels)}), got {max_level!r}")
    if enabled and levels[0] != base_spacing:
        raise ValueError("schedule.adaptive.levels_seconds[0] must equal "
                         "pace.start_spacing_seconds when the ladder is enabled")
    window = _adaptive_int(adaptive, "protect_window_requests", 20)
    min_requests = _adaptive_int(adaptive, "protect_min_requests", 10)
    if min_requests > window:
        raise ValueError("schedule.adaptive.protect_min_requests must be <= "
                         "protect_window_requests")
    drop_rate = _adaptive_ratio(adaptive, "protect_drop_rate", 0.30)
    drop_two = _adaptive_ratio(adaptive, "protect_drop_two_rate", 0.50)
    if drop_two < drop_rate:
        raise ValueError("schedule.adaptive.protect_drop_two_rate must be >= protect_drop_rate")
    streak = _adaptive_int(adaptive, "protect_drop_streak", 4)
    if streak > window:
        raise ValueError("schedule.adaptive.protect_drop_streak must be <= "
                         "protect_window_requests")
    first_hours = _adaptive_int(adaptive, "memory_first_hours", 2)
    max_hours = _adaptive_int(adaptive, "memory_max_hours", 24)
    if max_hours < first_hours:
        raise ValueError("schedule.adaptive.memory_max_hours must be >= memory_first_hours")
    demand_min = _adaptive_int(adaptive, "demand_min_waiting", 1000, minimum=0)
    demand_ease = _adaptive_int(adaptive, "demand_ease_waiting", 250, minimum=0)
    if demand_ease > demand_min:
        raise ValueError("schedule.adaptive.demand_ease_waiting must be <= demand_min_waiting")
    demand_hold = _adaptive_int(adaptive, "demand_hold_above_3x_waiting", 250, minimum=0)
    if demand_hold > demand_min:
        raise ValueError("schedule.adaptive.demand_hold_above_3x_waiting must be <= "
                         "demand_min_waiting")
    emergency_spacing = adaptive.get("emergency_spacing_seconds", 150.0)
    if (not isinstance(emergency_spacing, (int, float)) or isinstance(emergency_spacing, bool)
            or emergency_spacing <= levels[-1]):
        raise ValueError("schedule.adaptive.emergency_spacing_seconds must be a number "
                         f"> levels_seconds[-1] ({levels[-1]}), got {emergency_spacing!r}")
    return AdaptivePolicy(
        enabled=enabled,
        levels_seconds=levels,
        max_level=max_level,
        protect_window_requests=window,
        protect_min_requests=min_requests,
        protect_drop_rate=drop_rate,
        protect_drop_two_rate=drop_two,
        protect_drop_streak=streak,
        climb_min_requests=_adaptive_int(adaptive, "climb_min_requests", 200),
        climb_min_requests_above_3x=_adaptive_int(
            adaptive, "climb_min_requests_above_3x", 600),
        above_3x_climbs_per_day=_adaptive_int(adaptive, "above_3x_climbs_per_day", 1),
        rolling_24h_ceiling_requests=_adaptive_int(
            adaptive, "rolling_24h_ceiling_requests", 5000),
        demand_hold_above_3x_waiting=demand_hold,
        llm_above_4x_stop_share=_adaptive_ratio(adaptive, "llm_above_4x_stop_share", 0.75),
        above_3x_hold_hours=_adaptive_int(adaptive, "above_3x_hold_hours", 24),
        climb_max_upper_refusal_rate=_adaptive_ratio(
            adaptive, "climb_max_upper_refusal_rate", 0.12),
        climb_max_gap_seconds=_adaptive_int(adaptive, "climb_max_gap_seconds", 3600,
                                            minimum=60),
        memory_first_hours=first_hours,
        memory_max_hours=max_hours,
        clean_day_min_requests=_adaptive_int(adaptive, "clean_day_min_requests", 100,
                                             minimum=0),
        demand_min_waiting=demand_min,
        demand_ease_waiting=demand_ease,
        demand_ease_interval_seconds=_adaptive_int(
            adaptive, "demand_ease_interval_seconds", 3600),
        signal_refresh_seconds=_adaptive_int(adaptive, "signal_refresh_seconds", 300),
        llm_climb_stop_share=_adaptive_ratio(adaptive, "llm_climb_stop_share", 0.8),
        emergency_spacing_seconds=float(emergency_spacing),
        emergency_clear_requests=_adaptive_int(adaptive, "emergency_clear_requests", 20),
        emergency_connection_errors=_adaptive_int(
            adaptive, "emergency_connection_errors", 3),
        emergency_connection_window_seconds=_adaptive_int(
            adaptive, "emergency_connection_window_seconds", 600),
        pause_after_block_seconds=_adaptive_int(adaptive, "pause_after_block_seconds", 3600,
                                                minimum=60),
    )


def load_schedule_config(path: str = SCHEDULE_CONFIG_PATH) -> ScheduleConfig:
    """Read and validate config/schedule.json. A malformed value raises here, at
    service start, rather than turning into an unbounded or always-closed gate."""
    with open(path) as handle:
        data = json.load(handle)
    timezone_name = data.get("timezone")
    if not isinstance(timezone_name, str) or not timezone_name:
        raise ValueError("schedule.timezone must be a non-empty string")
    try:
        ZoneInfo(timezone_name)
    except Exception as exc:  # noqa: BLE001 - any ZoneInfo failure is a bad timezone
        raise ValueError(f"schedule.timezone {timezone_name!r} is not a known timezone: {exc}") from exc

    closed = data.get("closed_hours")
    if "closed_hours" not in data:
        raise ValueError("schedule.closed_hours must be present; use null for no closed window")
    closed_hours: tuple[int, int] | None = None
    if closed is not None:
        if (not isinstance(closed, list) or len(closed) != 2
                or not all(isinstance(h, int) and not isinstance(h, bool) for h in closed)
                or not 0 <= closed[0] < closed[1] <= 24 or closed[1] - closed[0] >= 24):
            raise ValueError("schedule.closed_hours must be null (no closed window) or [start, end] "
                             "with 0 <= start < end <= 24 and a window shorter than 24 hours")
        closed_hours = (closed[0], closed[1])

    if data.pop("daily_caps", None) is not None:
        # A stale or rolled-back config may still carry the removed caps: ignore them and
        # never fail, so an old config can never stop the service.
        print("[schedule] config/schedule.json still carries the removed daily_caps block; "
              "it is ignored, delete it")

    if "breaker" in data:
        raise ValueError("schedule.breaker was removed (design 2026-10-16); delete it")
    taper = data.get("weekend_taper_min_interval_hours")
    if taper is not None:
        raise ValueError("schedule.weekend_taper_min_interval_hours was removed "
                         "(design 2026-10-16); delete it")
    pace = _load_pace(data)
    return ScheduleConfig(
        timezone=timezone_name,
        closed_hours=closed_hours,
        pace=pace,
        refusals=_load_refusals(data),
        adaptive=_load_adaptive(data, pace.start_spacing_seconds),
    )


def local_now(schedule: ScheduleConfig, now: datetime | None = None) -> datetime:
    return (now or utcnow()).astimezone(schedule.zone)


def local_day(schedule: ScheduleConfig, now: datetime | None = None) -> date:
    """The Rome calendar day. LinkedIn's budget resets at Rome midnight, not UTC."""
    return local_now(schedule, now).date()


def in_closed_window(schedule: ScheduleConfig, now: datetime | None = None) -> bool:
    """Rome hours [start, end) when config sets a closed window, else False.
    The shipped config sets none, so LinkedIn runs around the clock."""
    if schedule.closed_hours is None:
        return False
    start, end = schedule.closed_hours
    return start <= local_now(schedule, now).hour < end


def requests_today_for(day: date | None, count: int, schedule: ScheduleConfig,
                       now: datetime | None = None) -> int:
    """A stored counter is only today's counter when the stored day is today."""
    return count if day == local_day(schedule, now) else 0


def start_spacing_seconds(schedule: ScheduleConfig) -> float:
    return schedule.pace.start_spacing_seconds


def open_seconds_between(schedule: ScheduleConfig, start: datetime, end: datetime) -> float:
    """Elapsed seconds in [start, end) minus the closed window each day it falls in.
    With no closed window that is simply the wall time. A closed window's open spans are
    [0, closed_start) and [closed_end, 24), built with
    `datetime.combine(day, time(h), tzinfo=zone)` so DST is handled by zoneinfo."""
    if end <= start:
        return 0.0
    if schedule.closed_hours is None:
        return (end - start).total_seconds()
    zone = schedule.zone
    closed_start, closed_end = schedule.closed_hours
    local_start, local_end = start.astimezone(zone), end.astimezone(zone)
    total = 0.0
    day = local_start.date()
    while day <= local_end.date():
        for span_start_hour, span_end_hour in ((0, closed_start), (closed_end, HOURS_PER_DAY)):
            if span_start_hour >= span_end_hour:
                continue
            span_start = datetime.combine(day, time(span_start_hour), tzinfo=zone)
            span_end = (datetime.combine(day, time(0), tzinfo=zone) + timedelta(days=1)
                        if span_end_hour >= HOURS_PER_DAY
                        else datetime.combine(day, time(span_end_hour), tzinfo=zone))
            overlap_start, overlap_end = max(span_start, start), min(span_end, end)
            if overlap_end > overlap_start:
                total += (overlap_end - overlap_start).total_seconds()
        day += timedelta(days=1)
    return total
