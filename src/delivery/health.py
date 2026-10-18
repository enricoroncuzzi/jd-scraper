"""Service health (design section 4.7): what the day summary shows and what the
watchdog alerts on.

Three checks remain: a service that has not beaten its heart in over an hour (or,
for a timer-driven source, past its interval plus slack), a daily source whose
last run ended in error, and LinkedIn having no successful request for 24 h of
open time. A fired alert repeats on its own cadence and says so once when it
clears; the ledger lives in kv_state.

The old all-source "nothing collected for six hours" check is gone. The LinkedIn
silence counts only open time: with the shipped config there is no closed window,
so open time is wall time; a restored window is subtracted as before.
"""
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from src.jobstore import heartbeat
from src.delivery.settings import DeliverySettings
from src.linkedin.schedule import ScheduleConfig, load_schedule_config, open_seconds_between

SERVICE_DOWN_AFTER = timedelta(hours=1)
REPEAT_ALERT_AFTER = timedelta(hours=6)

# Where the operator lists extra sources, and how the watchdog and the day summary learn about
# them: one source of truth for both, so the summary cannot disagree with the watchdog.
SOURCES_CONFIG_PATH = "config/sources.json"
SOURCE_SILENCE_SLACK_HOURS = 3


@dataclass(frozen=True)
class HealthIssue:
    key: str
    message: str


def _enabled_source_configs(path: str) -> dict[str, dict]:
    try:
        with open(path) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return {name: cfg for name, cfg in data.items() if isinstance(cfg, dict) and cfg.get("enabled")}


def enabled_sources(path: str | None = None) -> list[str]:
    """Extra sources switched on in config/sources.json; none when the file is absent."""
    return sorted(_enabled_source_configs(path or SOURCES_CONFIG_PATH))


def expected_services(settings: DeliverySettings, sources: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(settings.expected_services + tuple(f"collect-{name}" for name in sources)))


def source_silence_allowances(path: str | None = None) -> dict[str, timedelta]:
    """The timer-driven collectors beat once a day, so health allows each one its interval
    plus a few hours of silence instead of the one-hour always-on limit."""
    return {f"collect-{name}": timedelta(hours=int(cfg.get("interval_hours", 24)) + SOURCE_SILENCE_SLACK_HOURS)
            for name, cfg in _enabled_source_configs(path or SOURCES_CONFIG_PATH).items()}


def age_text(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    return f"{minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def service_issues(conn, settings: DeliverySettings, now: datetime,
                   silence_allowance: dict[str, timedelta] | None = None) -> list[HealthIssue]:
    allowances = silence_allowance or {}
    beats = {beat.service: beat for beat in heartbeat.read_all(conn)}
    issues = []
    for name in settings.expected_services:
        beat = beats.get(name)
        if beat is None:
            issues.append(HealthIssue(f"service:{name}", f"{name} has never reported in"))
        elif now - beat.beat_at > allowances.get(name, SERVICE_DOWN_AFTER):
            issues.append(HealthIssue(
                f"service:{name}", f"{name} has been silent for {age_text(now - beat.beat_at)} "
                                   f"(last state: {beat.state})"))
        elif name in allowances and beat.state == "error":
            issues.append(HealthIssue(f"service:{name}",
                                      f"{name}: last run ended in error {beat.detail}"))
    return issues


def last_linkedin_success(conn) -> datetime | None:
    """The last successful LinkedIn request: the pace row's `last_clean_at`, else the
    newest `ok` in pace_events, else None when nothing has ever succeeded."""
    with conn.cursor() as cur:
        cur.execute("SELECT last_clean_at FROM pace_budgets WHERE host = 'linkedin.com'")
        row = cur.fetchone()
        if row is not None and row[0] is not None:
            return row[0]
        cur.execute("SELECT max(at) FROM pace_events WHERE outcome = 'ok'")
        return cur.fetchone()[0]


def linkedin_blocked_issue(conn, settings: DeliverySettings, now: datetime,
                           schedule: ScheduleConfig | None = None) -> HealthIssue | None:
    """The one alert this design adds: a full day of open time with no successful
    LinkedIn request. With no closed window, open time is wall time. None when no
    LinkedIn request has ever succeeded (the service check covers that)."""
    last = last_linkedin_success(conn)
    if last is None:
        return None
    schedule = schedule or load_schedule_config()
    open_time = timedelta(seconds=open_seconds_between(schedule, last, now))
    if open_time < timedelta(hours=settings.linkedin_blocked_after_open_hours):
        return None
    last_rome = last.astimezone(schedule.zone).strftime("%Y-%m-%d %H:%M")
    return HealthIssue("linkedin_blocked",
                       f"LinkedIn: no request has succeeded for {age_text(open_time)} of open time "
                       f"(last success {last_rome} Rome)")


def evaluate_health(conn, settings: DeliverySettings, now: datetime,
                    silence_allowance: dict[str, timedelta] | None = None,
                    schedule: ScheduleConfig | None = None) -> list[HealthIssue]:
    issues = service_issues(conn, settings, now, silence_allowance)
    blocked = linkedin_blocked_issue(conn, settings, now, schedule)
    return issues + ([blocked] if blocked else [])


def health_lines(conn, settings: DeliverySettings, now: datetime,
                 silence_allowance: dict[str, timedelta] | None = None) -> list[str]:
    """One line per expected service for the day summary."""
    allowances = silence_allowance or {}
    beats = {beat.service: beat for beat in heartbeat.read_all(conn)}
    lines = []
    for name in settings.expected_services:
        beat = beats.get(name)
        if beat is None:
            lines.append(f"{name}: never reported")
        else:
            down = now - beat.beat_at > allowances.get(name, SERVICE_DOWN_AFTER)
            lines.append(f"{name}: {'DOWN' if down else beat.state}, last beat {age_text(now - beat.beat_at)} ago, "
                         f"up {age_text(now - beat.started_at)}")
    return lines


def repeat_after(key: str, settings: DeliverySettings) -> timedelta:
    """How long before one alert key fires again: the LinkedIn block at most once a
    day, every other alert every six hours."""
    if key == "linkedin_blocked":
        return timedelta(hours=settings.linkedin_blocked_repeat_hours)
    return REPEAT_ALERT_AFTER


def _ledger(conn) -> dict[str, datetime]:
    with conn.cursor() as cur:
        cur.execute("SELECT key, value FROM kv_state WHERE key LIKE 'health:%%'")
        return {key[len("health:"):]: datetime.fromisoformat(value["sent_at"]) for key, value in cur.fetchall()}


def run_health_check(conn, settings: DeliverySettings, send, now: datetime,
                     silence_allowance: dict[str, timedelta] | None = None) -> list[str]:
    """Send alerts for new or long-running issues and a note for cleared ones. Returns
    the messages sent. A failed send is retried on the next check; a service alert
    repeats every six hours and the LinkedIn silence at most once a day. Any leftover
    ledger row from an older check is cleared once with an "OK again" note."""
    issues = {issue.key: issue for issue in evaluate_health(conn, settings, now, silence_allowance)}
    ledger = _ledger(conn)
    sent = []
    with conn.cursor() as cur:
        for key, issue in issues.items():
            if key in ledger and now - ledger[key] < repeat_after(key, settings):
                continue
            text = f"ALERT: {issue.message}"
            if send(text) is False:
                continue
            cur.execute(
                "INSERT INTO kv_state (key, value, updated_at) VALUES (%s, %s::jsonb, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at",
                (f"health:{key}", json.dumps({"sent_at": now.isoformat()}), now))
            sent.append(text)
        for key in ledger:
            if key not in issues:
                text = f"OK again: {key.replace(':', ' ')} is healthy"
                if send(text) is False:
                    continue
                cur.execute("DELETE FROM kv_state WHERE key = %s", (f"health:{key}",))
                sent.append(text)
    conn.commit()
    return sent
