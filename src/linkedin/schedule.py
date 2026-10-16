"""The LinkedIn schedule: quiet hours, the shared daily request budget and the
steady-pace policy, loaded from config/schedule.json (spec section 4).

The collector and the processor share one PaceBudget on host `linkedin.com`, so
this module holds only the policy: what the Rome clock and the per-day counters
mean, and the numbers the pace is built from. Enforcement lives in
src/linkedin/pace.py, the single choke point every LinkedIn request passes through.
"""
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from src.jobstore.db import utcnow

SCHEDULE_CONFIG_PATH = "config/schedule.json"
HOURS_PER_DAY = 24


@dataclass(frozen=True)
class PacePolicy:
    """The steady pace (design section 4.1): where it starts, how far it may
    loosen, and how a finished day tightens or loosens it."""

    start_weekday_seconds: float = 75.0
    start_weekend_seconds: float = 110.0
    jitter: float = 0.2
    max_spacing_factor: float = 2.0
    calm_days_to_tighten: int = 3
    calm_refusal_rate: float = 0.02
    loosen_refusal_rate: float = 0.10
    step: float = 0.10
    min_day_requests: int = 100


@dataclass(frozen=True)
class RefusalPolicy:
    """What a refusal costs (design section 4.2): a capped, doubling wait and the
    hour-level nudge when refusals crowd an hour."""

    first_wait_seconds: float = 60.0
    max_wait_seconds: float = 1800.0
    hour_refusal_rate: float = 0.10
    hour_multiplier: float = 1.5
    hour_max_multiplier: float = 3.0


@dataclass(frozen=True)
class ScheduleConfig:
    timezone: str = "Europe/Rome"
    closed_hours: tuple[int, int] = (0, 6)
    weekday_cap: int = 1400
    weekend_cap: int = 800
    pace: PacePolicy = PacePolicy()
    refusals: RefusalPolicy = RefusalPolicy()

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def _require_int(data: dict, key: str, where: str, *, minimum: int) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{where}.{key} must be an integer >= {minimum}, got {value!r}")
    return value


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


def _load_pace(data: dict) -> PacePolicy:
    pace = data.get("pace")
    if not isinstance(pace, dict):
        raise ValueError("schedule.pace must be an object")
    starts = pace.get("start_spacing_seconds")
    if not isinstance(starts, dict):
        raise ValueError("schedule.pace.start_spacing_seconds must be an object")
    weekday = _require_number(starts, "weekday", "schedule.pace.start_spacing_seconds", exclusive=True)
    weekend = _require_number(starts, "weekend", "schedule.pace.start_spacing_seconds", exclusive=True)
    jitter = _require_ratio(pace, "jitter", "schedule.pace", maximum=0.5, exclusive_max=True)
    max_factor = _require_number(pace, "max_spacing_factor", "schedule.pace", minimum=1.0)
    tighten = pace.get("tighten")
    if not isinstance(tighten, dict):
        raise ValueError("schedule.pace.tighten must be an object")
    calm_rate = _require_ratio(tighten, "calm_refusal_rate", "schedule.pace.tighten", exclusive_min=True)
    loosen_rate = _require_ratio(tighten, "loosen_refusal_rate", "schedule.pace.tighten", exclusive_min=True)
    if calm_rate >= loosen_rate:
        raise ValueError("schedule.pace.tighten.calm_refusal_rate must be below loosen_refusal_rate")
    return PacePolicy(
        start_weekday_seconds=weekday,
        start_weekend_seconds=weekend,
        jitter=jitter,
        max_spacing_factor=max_factor,
        calm_days_to_tighten=_require_int(tighten, "calm_days", "schedule.pace.tighten", minimum=1),
        calm_refusal_rate=calm_rate,
        loosen_refusal_rate=loosen_rate,
        step=_require_ratio(tighten, "step", "schedule.pace.tighten", exclusive_min=True, exclusive_max=True),
        min_day_requests=_require_int(tighten, "min_day_requests", "schedule.pace.tighten", minimum=1),
    )


def _load_refusals(data: dict) -> RefusalPolicy:
    refusals = data.get("refusals")
    if not isinstance(refusals, dict):
        raise ValueError("schedule.refusals must be an object")
    first = _require_number(refusals, "first_wait_seconds", "schedule.refusals", exclusive=True)
    maximum = _require_number(refusals, "max_wait_seconds", "schedule.refusals", exclusive=True)
    if first > maximum:
        raise ValueError("schedule.refusals.max_wait_seconds must be at least first_wait_seconds")
    rate = _require_ratio(refusals, "hour_refusal_rate", "schedule.refusals", exclusive_min=True)
    multiplier = _require_number(refusals, "hour_multiplier", "schedule.refusals", minimum=1.0)
    max_multiplier = _require_number(refusals, "hour_max_multiplier", "schedule.refusals", minimum=1.0)
    if multiplier > max_multiplier:
        raise ValueError("schedule.refusals.hour_multiplier must not exceed hour_max_multiplier")
    return RefusalPolicy(first, maximum, rate, multiplier, max_multiplier)


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
    if (not isinstance(closed, list) or len(closed) != 2
            or not all(isinstance(h, int) and not isinstance(h, bool) for h in closed)
            or not 0 <= closed[0] < closed[1] <= 24 or closed[1] - closed[0] >= 24):
        raise ValueError("schedule.closed_hours must be [start, end] with 0 <= start < end <= 24 "
                         "and a window shorter than 24 hours")

    caps = data.get("daily_caps")
    if not isinstance(caps, dict):
        raise ValueError("schedule.daily_caps must be an object")
    weekday_cap = _require_int(caps, "weekday", "schedule.daily_caps", minimum=1)
    weekend_cap = _require_int(caps, "weekend", "schedule.daily_caps", minimum=1)

    if "breaker" in data:
        raise ValueError("schedule.breaker was removed (design 2026-10-16); delete it")
    taper = data.get("weekend_taper_min_interval_hours")
    if taper is not None:
        raise ValueError("schedule.weekend_taper_min_interval_hours was removed "
                         "(design 2026-10-16); delete it")
    return ScheduleConfig(
        timezone=timezone_name,
        closed_hours=(closed[0], closed[1]),
        weekday_cap=weekday_cap,
        weekend_cap=weekend_cap,
        pace=_load_pace(data),
        refusals=_load_refusals(data),
    )


def local_now(schedule: ScheduleConfig, now: datetime | None = None) -> datetime:
    return (now or utcnow()).astimezone(schedule.zone)


def local_day(schedule: ScheduleConfig, now: datetime | None = None) -> date:
    """The Rome calendar day. LinkedIn's budget resets at Rome midnight, not UTC."""
    return local_now(schedule, now).date()


def is_weekend(schedule: ScheduleConfig, now: datetime | None = None) -> bool:
    return local_now(schedule, now).weekday() >= 5


def window_name(schedule: ScheduleConfig, now: datetime | None = None) -> str:
    return "weekend" if is_weekend(schedule, now) else "weekday"


def daily_cap(schedule: ScheduleConfig, now: datetime | None = None) -> int:
    """The shared search+description request cap for the Rome day `now` falls in."""
    return schedule.weekend_cap if is_weekend(schedule, now) else schedule.weekday_cap


def in_closed_window(schedule: ScheduleConfig, now: datetime | None = None) -> bool:
    """Rome hours [start, end). 00:00-06:00 ships in config/schedule.json."""
    start, end = schedule.closed_hours
    return start <= local_now(schedule, now).hour < end


def requests_today_for(day: date | None, count: int, schedule: ScheduleConfig,
                       now: datetime | None = None) -> int:
    """A stored counter is only today's counter when the stored day is today."""
    return count if day == local_day(schedule, now) else 0


def open_hours_per_day(schedule: ScheduleConfig) -> int:
    """Hours of every day the gate is open, from the quiet window in config."""
    start, end = schedule.closed_hours
    return HOURS_PER_DAY - (end - start)


def _cap_for_window(schedule: ScheduleConfig, window: str) -> int:
    return schedule.weekend_cap if window == "weekend" else schedule.weekday_cap


def floor_spacing_seconds(schedule: ScheduleConfig, window: str) -> float:
    """The fastest steady pace the cap allows: open seconds divided by the cap."""
    return open_hours_per_day(schedule) * 3600 / _cap_for_window(schedule, window)


def start_spacing_seconds(schedule: ScheduleConfig, window: str) -> float:
    return (schedule.pace.start_weekend_seconds if window == "weekend"
            else schedule.pace.start_weekday_seconds)


def open_seconds_between(schedule: ScheduleConfig, start: datetime, end: datetime) -> float:
    """Open seconds in [start, end): the elapsed time minus the quiet window each day
    it falls in. Each day's open spans are [0, closed_start) and [closed_end, 24), built
    with `datetime.combine(day, time(h), tzinfo=zone)` so DST is handled by zoneinfo."""
    if end <= start:
        return 0.0
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
