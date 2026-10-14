"""Service health (spec section 8): what the day summary shows and what the
watchdog alerts on.

An alert fires when a service has not beaten its heart for over an hour, or when
nothing was collected for six hours. A fired alert repeats every six hours while
the problem lasts and says so once when it clears; the ledger lives in kv_state.
"""
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from src.jobstore import heartbeat
from src.delivery.settings import DeliverySettings

SERVICE_DOWN_AFTER = timedelta(hours=1)
COLLECTION_SILENT_AFTER = timedelta(hours=6)
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


def _age(delta: timedelta) -> str:
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
                f"service:{name}", f"{name} has been silent for {_age(now - beat.beat_at)} "
                                   f"(last state: {beat.state})"))
        elif name in allowances and beat.state == "error":
            issues.append(HealthIssue(f"service:{name}",
                                      f"{name}: last run ended in error {beat.detail}"))
    return issues


def last_collection(conn) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute("SELECT max(last_seen_at) FROM sightings")
        return cur.fetchone()[0]


def collection_issue(conn, now: datetime) -> HealthIssue | None:
    last = last_collection(conn)
    if last is None or now - last > COLLECTION_SILENT_AFTER:
        since = "ever" if last is None else f"for {_age(now - last)}"
        return HealthIssue("collection", f"Nothing has been collected {since}")
    return None


def evaluate_health(conn, settings: DeliverySettings, now: datetime,
                    silence_allowance: dict[str, timedelta] | None = None) -> list[HealthIssue]:
    issues = service_issues(conn, settings, now, silence_allowance)
    silent = collection_issue(conn, now)
    return issues + ([silent] if silent else [])


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
            lines.append(f"{name}: {'DOWN' if down else beat.state}, last beat {_age(now - beat.beat_at)} ago, "
                         f"up {_age(now - beat.started_at)}")
    return lines


def _ledger(conn) -> dict[str, datetime]:
    with conn.cursor() as cur:
        cur.execute("SELECT key, value FROM kv_state WHERE key LIKE 'health:%%'")
        return {key[len("health:"):]: datetime.fromisoformat(value["sent_at"]) for key, value in cur.fetchall()}


def run_health_check(conn, settings: DeliverySettings, send, now: datetime,
                     silence_allowance: dict[str, timedelta] | None = None) -> list[str]:
    """Send alerts for new or long-running issues and a note for cleared ones.
    Returns the messages sent. A failed send is retried on the next check."""
    issues = {issue.key: issue for issue in evaluate_health(conn, settings, now, silence_allowance)}
    ledger = _ledger(conn)
    sent = []
    with conn.cursor() as cur:
        for key, issue in issues.items():
            if key in ledger and now - ledger[key] < REPEAT_ALERT_AFTER:
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
