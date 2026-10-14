"""Shared row access and stage transitions for the processor stages.

Every transition is one UPDATE inside the caller's transaction, so a crash
leaves the job exactly where it was and the stage runs again after a restart.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

_COLUMNS = (
    "id", "title", "company", "location_raw", "canonical_url", "description",
    "description_status", "work_mode", "remote_hint", "country", "tier", "tier_hint", "stage",
    "remote_verdict", "attempts", "first_seen_at", "posted_at", "stage_changed_at",
    "first_refused_at",
)


@dataclass
class JobRow:
    id: str
    title: str
    company: str
    location_raw: str
    canonical_url: str
    description: str
    description_status: str
    work_mode: str
    remote_hint: str | None
    country: str | None
    tier: int | None
    tier_hint: int | None
    stage: str
    remote_verdict: str | None
    attempts: int
    first_seen_at: datetime
    posted_at: datetime | None
    stage_changed_at: datetime
    first_refused_at: datetime | None


SELECT_JOB = f"SELECT {', '.join(_COLUMNS)} FROM jobs"


def load_job(cur, job_id: str) -> JobRow | None:
    cur.execute(f"{SELECT_JOB} WHERE id = %s", (job_id,))
    row = cur.fetchone()
    return JobRow(*row) if row else None


def set_stage(cur, job_id: str, stage: str, reason: str, now: datetime) -> None:
    cur.execute(
        "UPDATE jobs SET stage = %s, stage_reason = %s, stage_changed_at = %s WHERE id = %s",
        (stage, reason, now, job_id))


def defer(cur, job_id: str, now: datetime, delay: timedelta) -> None:
    """Try again later: count the attempt and move next_attempt_at out."""
    cur.execute(
        "UPDATE jobs SET attempts = attempts + 1, next_attempt_at = %s WHERE id = %s",
        (now + delay, job_id))
