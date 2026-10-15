"""The LinkedIn schedule: quiet hours, the shared daily request budget and the
circuit-breaker ladder, loaded from config/schedule.json (spec section 4).

The collector and the processor share one PaceBudget on host `linkedin.com`, so
this module holds only the policy: what the Rome clock and the per-day counters
mean. Enforcement lives in src/linkedin/pace.py, the single choke point every
LinkedIn request passes through.
"""
import json
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from src.jobstore.db import utcnow

SCHEDULE_CONFIG_PATH = "config/schedule.json"


@dataclass(frozen=True)
class ScheduleConfig:
    timezone: str = "Europe/Rome"
    closed_hours: tuple[int, int] = (0, 6)
    weekday_cap: int = 1400
    weekend_cap: int = 800
    breaker_refusals: int = 3
    breaker_window_minutes: int = 30
    rest_ladder_hours: tuple[int, ...] = (2, 6, 24)
    weekend_taper_min_interval_hours: int = 24

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def _require_int(data: dict, key: str, where: str, *, minimum: int) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{where}.{key} must be an integer >= {minimum}, got {value!r}")
    return value


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

    breaker = data.get("breaker")
    if not isinstance(breaker, dict):
        raise ValueError("schedule.breaker must be an object")
    refusals = _require_int(breaker, "refusals", "schedule.breaker", minimum=1)
    window_minutes = _require_int(breaker, "window_minutes", "schedule.breaker", minimum=1)
    ladder = breaker.get("rest_ladder_hours")
    if (not isinstance(ladder, list) or not ladder
            or not all(isinstance(h, int) and not isinstance(h, bool) and h >= 1 for h in ladder)):
        raise ValueError("schedule.breaker.rest_ladder_hours must be a non-empty list of positive integers")

    taper = _require_int(data, "weekend_taper_min_interval_hours", "schedule", minimum=1)
    return ScheduleConfig(
        timezone=timezone_name,
        closed_hours=(closed[0], closed[1]),
        weekday_cap=weekday_cap,
        weekend_cap=weekend_cap,
        breaker_refusals=refusals,
        breaker_window_minutes=window_minutes,
        rest_ladder_hours=tuple(ladder),
        weekend_taper_min_interval_hours=taper,
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
