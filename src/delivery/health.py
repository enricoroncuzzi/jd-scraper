"""Service health (design section 4.7): what the day summary shows and what the
watchdog alerts on.

Checks: a service that has not beaten its heart in over an hour (or, for a
timer-driven source, past its interval plus slack), a daily source whose last run
ended in error, and LinkedIn having no successful request for 24 h of open time. A
fired alert repeats on its own cadence and says so once when it clears; the ledger
lives in kv_state.

The old all-source "nothing collected for six hours" check is gone. The LinkedIn
silence counts only open time: with the shipped config there is no closed window,
so open time is wall time; a restored window is subtracted as before.

The 2026-10-29 "went quiet" seatbelt adds the three silent stalls no other alert
can see, each alerted at most once a day: successful LinkedIn requests that yield
no new job (a page-layout or result change), a model stage whose calls produce no
usable answer (every model failing, or only empty output, which the fallback guard
leaves out because it counts answers only), and no job reaching the scored stage (a
stall in or upstream of scoring, while every service keeps beating).
"""
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from src.jobstore import heartbeat
from src.delivery.settings import DeliverySettings
from src.linkedin.adaptive import normalize_state, paused_day
from src.linkedin.schedule import (
    ScheduleConfig, load_schedule_config, local_day, open_seconds_between,
)

SERVICE_DOWN_AFTER = timedelta(hours=1)
REPEAT_ALERT_AFTER = timedelta(hours=6)

# The "went quiet" thresholds (2026-10-29 seatbelt), each a full day except a model
# stage, where six hours without a single answer is already past every retry.
LINKEDIN_NEW_JOB_SILENCE_AFTER = timedelta(hours=24)
LLM_STAGE_ANSWER_SILENCE_AFTER = timedelta(hours=6)
SCORING_SILENCE_AFTER = timedelta(hours=24)
# A job reached `scored` while its stage is one of these: `expired` is left out because
# the age-out retires `seen` jobs there without ever scoring them.
SCORED_STAGES = ("scored", "delivered")
LLM_STAGE_ISSUE_PREFIX = "llm_stage:"
# Alerts that fire at most once a day: the LinkedIn silence, pause and quiet feed, a
# silent model stage and a stalled scoring stage would otherwise repeat every six hours.
ONCE_A_DAY_ISSUES = ("linkedin_blocked", "linkedin_paused", "linkedin_no_new_jobs", "scoring_silent")

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


def linkedin_paused_issue(conn, settings: DeliverySettings, now: datetime,
                          schedule: ScheduleConfig | None = None) -> HealthIssue | None:
    """One alert when the adaptive ladder paused LinkedIn for the rest of the Rome day.
    None with the ladder off, a missing row or a state that is not a paused day."""
    schedule = schedule or load_schedule_config()
    if not schedule.adaptive.enabled:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT adaptive_state FROM pace_budgets WHERE host = 'linkedin.com'")
        row = cur.fetchone()
    if row is None:
        return None
    state = normalize_state(row[0], schedule.adaptive)
    if paused_day(state) != local_day(schedule, now):
        return None
    reason = (state.get("paused") or {}).get("reason", "block")
    return HealthIssue("linkedin_paused",
                       f"LinkedIn paused for the rest of the day (block persisted 1h: {reason})")


def linkedin_new_job_issue(conn, now: datetime,
                           schedule: ScheduleConfig | None = None) -> HealthIssue | None:
    """LinkedIn answers requests but yields no new job: a page-layout or result change
    that the request-success check cannot see. None when LinkedIn never yielded a job
    (the service check covers that) and None while requests are refused, so a blocked
    or paused LinkedIn stays one alert."""
    with conn.cursor() as cur:
        cur.execute("SELECT max(seen_at) FROM sightings WHERE source = 'linkedin'")
        last = cur.fetchone()[0]
        if last is None:
            return None
        cur.execute("SELECT count(*) FROM pace_events WHERE outcome = 'ok' AND at >= %s",
                    (now - LINKEDIN_NEW_JOB_SILENCE_AFTER,))
        succeeded = cur.fetchone()[0]
    quiet = now - last
    if not succeeded or quiet < LINKEDIN_NEW_JOB_SILENCE_AFTER:
        return None
    schedule = schedule or load_schedule_config()
    last_rome = last.astimezone(schedule.zone).strftime("%Y-%m-%d %H:%M")
    return HealthIssue("linkedin_no_new_jobs",
                       f"LinkedIn: no new job in {age_text(quiet)} while requests still succeed "
                       f"(last new job {last_rome} Rome)")


def llm_stage_issues(conn, now: datetime) -> list[HealthIssue]:
    """A stage whose model calls have produced no usable answer for six hours: its first
    call after its last answer is that old and it is still being called. The fallback
    guard counts answers only, so every model failing is invisible there; here it is one
    issue per stage. A stage that stops being called clears within the same six hours, so
    a retired stage name cannot alert forever. The read covers twice the threshold: a
    longer streak shows a shorter age, still past the threshold. A call counts as answered
    only when it ended `ok` with a model named: a provider that keeps returning empty or
    malformed structured output (`invalid_output`) has answered nothing either."""
    since = now - 2 * LLM_STAGE_ANSWER_SILENCE_AFTER
    threshold = now - LLM_STAGE_ANSWER_SILENCE_AFTER
    with conn.cursor() as cur:
        cur.execute(
            "WITH last_answer AS ("
            " SELECT stage, max(started_at) AS at FROM llm_calls"
            " WHERE started_at >= %s AND outcome = 'ok' AND response_model IS NOT NULL"
            " GROUP BY stage) "
            "SELECT c.stage, count(*), min(c.started_at) FROM llm_calls c "
            "LEFT JOIN last_answer a ON a.stage = c.stage "
            "WHERE c.started_at >= %s AND (a.at IS NULL OR c.started_at > a.at) "
            "GROUP BY c.stage HAVING min(c.started_at) <= %s AND max(c.started_at) >= %s "
            "ORDER BY c.stage",
            (since, since, threshold, threshold))
        rows = cur.fetchall()
    return [HealthIssue(f"{LLM_STAGE_ISSUE_PREFIX}{stage}",
                        f"the {stage} stage made {calls} model call(s) in the last "
                        f"{age_text(now - first)} and none produced a usable answer")
            for stage, calls, first in rows]


def scoring_silence_issue(conn, now: datetime,
                          schedule: ScheduleConfig | None = None) -> HealthIssue | None:
    """No job reached the scored stage: the processor keeps beating and the cheap stages
    keep running, so nothing else shows that scoring stopped. None when no job has ever
    been scored (a fresh store; the service checks cover its start)."""
    with conn.cursor() as cur:
        cur.execute("SELECT max(stage_changed_at) FROM jobs WHERE stage = ANY(%s)",
                    (list(SCORED_STAGES),))
        last = cur.fetchone()[0]
    if last is None:
        return None
    quiet = now - last
    if quiet < SCORING_SILENCE_AFTER:
        return None
    schedule = schedule or load_schedule_config()
    last_rome = last.astimezone(schedule.zone).strftime("%Y-%m-%d %H:%M")
    return HealthIssue("scoring_silent",
                       f"no job has reached the scored stage in {age_text(quiet)} "
                       f"(last {last_rome} Rome)")


def evaluate_health(conn, settings: DeliverySettings, now: datetime,
                    silence_allowance: dict[str, timedelta] | None = None,
                    schedule: ScheduleConfig | None = None) -> list[HealthIssue]:
    schedule = schedule or load_schedule_config()
    issues = service_issues(conn, settings, now, silence_allowance)
    for issue in (linkedin_blocked_issue(conn, settings, now, schedule),
                  linkedin_paused_issue(conn, settings, now, schedule),
                  linkedin_new_job_issue(conn, now, schedule),
                  scoring_silence_issue(conn, now, schedule)):
        if issue:
            issues.append(issue)
    return issues + llm_stage_issues(conn, now)


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
    """How long before one alert key fires again: the LinkedIn block, the pause and the
    went-quiet checks at most once a day, every other alert every six hours."""
    if key in ONCE_A_DAY_ISSUES or key.startswith(LLM_STAGE_ISSUE_PREFIX):
        return timedelta(hours=settings.linkedin_blocked_repeat_hours)
    return REPEAT_ALERT_AFTER


def _ledger(conn) -> dict[str, datetime]:
    with conn.cursor() as cur:
        cur.execute("SELECT key, value FROM kv_state WHERE key LIKE 'health:%%'")
        return {key[len("health:"):]: datetime.fromisoformat(value["sent_at"]) for key, value in cur.fetchall()}


def run_health_check(conn, settings: DeliverySettings, send, now: datetime,
                     silence_allowance: dict[str, timedelta] | None = None,
                     schedule: ScheduleConfig | None = None) -> list[str]:
    """Send alerts for new or long-running issues and a note for cleared ones. Returns
    the messages sent. A failed send is retried on the next check; a service alert
    repeats every six hours and the LinkedIn silence at most once a day. Any leftover
    ledger row from an older check is cleared once with an "OK again" note."""
    issues = {issue.key: issue for issue in evaluate_health(
        conn, settings, now, silence_allowance, schedule)}
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
