"""Stage 2: get the description (spec section 5, step 2).

Sources that return text inline never reach this stage. For LinkedIn jobs the
guest fragment is fetched through the shared pace. A host throttle (429/999)
returns the job to the queue unchanged except for its `rate_limited` mark, with no
per-job penalty; job-specific refusals (401/403/503/504) and the unreadable-markup
path keep the 30 minutes doubling to 12 hours backoff. The job expires after 7
days of host refusals, or when the posting is over 30 days old. The title+company
fallback is never used as a description.
"""
import time
from datetime import datetime, timedelta

import requests

from src.linkedin.description import fetch_description
from src.processor.cheap import finish_with_description
from src.processor.config import ProcessorConfig
from src.processor.state import JobRow, defer, load_job, set_stage
from src.services.pulse import pulse

UNREADABLE_ATTEMPTS = 3


def refusal_delay(attempts: int, cfg: ProcessorConfig) -> timedelta:
    """Wait after the `attempts`-th refusal: 30 min, 1 h, 2 h ... capped at 12 h."""
    minutes = min(cfg.backoff_first_minutes * 2 ** max(attempts - 1, 0), cfg.backoff_cap_hours * 60)
    return timedelta(minutes=minutes)


def oldest_waiting_description(cur, now: datetime) -> datetime | None:
    """The `first_seen_at` of the oldest job fetchable for a description right now
    (the same predicate as `_next_job`), or None when none is waiting."""
    cur.execute(
        "SELECT min(first_seen_at) FROM jobs WHERE stage = 'seen' AND tier IS NOT NULL "
        "AND description_status IN ('pending', 'rate_limited') AND next_attempt_at <= %s", (now,))
    return cur.fetchone()[0]


def description_overdue(cur, cfg: ProcessorConfig, now: datetime) -> bool:
    """True when the oldest waiting job was first seen more than
    `description_deadline_hours` ago: an overdue description may spend more than one
    processor tick waiting for its turn."""
    oldest = oldest_waiting_description(cur, now)
    return oldest is not None and now - oldest > timedelta(hours=cfg.description_deadline_hours)


def expire_stale(conn, cfg: ProcessorConfig, now: datetime) -> int:
    """Expire jobs that waited too long. Returns how many."""
    statements = [
        ("description_refused",
         "stage = 'seen' AND description_status = 'rate_limited' AND first_refused_at < %s",
         now - timedelta(days=cfg.refusal_expiry_days)),
        ("posting_too_old",
         "stage IN ('seen', 'described') AND posted_at IS NOT NULL AND posted_at < %s",
         now - timedelta(days=cfg.posting_max_age_days)),
        ("waited_too_long",
         "stage IN ('seen', 'described') AND first_seen_at < %s",
         now - timedelta(days=cfg.posting_max_age_days)),
    ]
    total = 0
    with conn.cursor() as cur:
        for reason, condition, cutoff in statements:
            cur.execute(
                f"UPDATE jobs SET stage = 'expired', stage_reason = %s, stage_changed_at = %s "
                f"WHERE {condition}", (reason, now, cutoff))
            total += cur.rowcount
    return total


def _next_job(cur, now: datetime) -> JobRow | None:
    cur.execute(
        "SELECT id FROM jobs WHERE stage = 'seen' AND tier IS NOT NULL "
        "AND description_status IN ('pending', 'rate_limited') AND next_attempt_at <= %s "
        "ORDER BY tier, first_seen_at LIMIT 1", (now,))
    row = cur.fetchone()
    return load_job(cur, row[0]) if row else None


def _linkedin_id(cur, job_id) -> str | None:
    cur.execute("SELECT source_job_id FROM sightings WHERE job_id = %s AND source = 'linkedin' "
                "ORDER BY seen_at LIMIT 1", (job_id,))
    row = cur.fetchone()
    return row[0] if row else None


def describe_next(conn, pace, cfg: ProcessorConfig, now: datetime, *,
                  get=requests.get, sleep=time.sleep) -> str | None:
    """Fetch one description. Returns None when no job is waiting for one, else one
    of 'described', 'throttled', 'refused', 'gone' or 'unreadable'."""
    with conn.cursor() as cur:
        job = _next_job(cur, now)
        if job is None:
            return None
        overdue = description_overdue(cur, cfg, now)
        linkedin_id = _linkedin_id(cur, job.id)
        if linkedin_id is None:
            set_stage(cur, job.id, "expired", "no_description_source", now)
            conn.commit()
            return "gone"
    result = fetch_description(pace, linkedin_id, overdue=overdue, get=get, sleep=sleep)
    with conn.cursor() as cur:
        if result.status == "ok":
            cur.execute(
                "UPDATE jobs SET description = %s, description_status = 'ok', "
                "description_source = 'linkedin', first_refused_at = NULL, attempts = 0, "
                "posted_text = %s WHERE id = %s",
                (result.text, result.posted_text or None, job.id))
            job.description = result.text
            finish_with_description(cur, job, now)
            outcome = "described"
        elif result.status == "deferred":
            # The schedule gate closed (a configured closed window or the daily cap) between
            # requests. Leave the job exactly as it is; the next tick retries it.
            return "deferred"
        elif result.status == "throttled":
            # A host refusal (429/999): back to the queue with no per-job penalty. The
            # 7-day expiry still applies; attempts and next_attempt_at are untouched.
            cur.execute(
                "UPDATE jobs SET description_status = 'rate_limited', "
                "first_refused_at = COALESCE(first_refused_at, %s) WHERE id = %s", (now, job.id))
            outcome = "throttled"
        elif result.status == "refused":
            cur.execute(
                "UPDATE jobs SET description_status = 'rate_limited', "
                "first_refused_at = COALESCE(first_refused_at, %s) WHERE id = %s", (now, job.id))
            defer(cur, job.id, now, refusal_delay(job.attempts + 1, cfg))
            outcome = "refused"
        elif result.status == "gone":
            cur.execute("UPDATE jobs SET description_status = 'failed' WHERE id = %s", (job.id,))
            set_stage(cur, job.id, "expired", "posting_gone", now)
            outcome = "gone"
        else:
            if job.attempts + 1 >= UNREADABLE_ATTEMPTS:
                cur.execute("UPDATE jobs SET description_status = 'failed', attempts = attempts + 1 "
                            "WHERE id = %s", (job.id,))
                set_stage(cur, job.id, "expired", "description_unreadable", now)
            else:
                defer(cur, job.id, now, refusal_delay(job.attempts + 1, cfg))
            outcome = "unreadable"
    conn.commit()
    return outcome


def run_descriptions(conn, pace, cfg: ProcessorConfig, now: datetime, *,
                     get=requests.get, sleep=time.sleep, clock=time.monotonic,
                     skip_if_wait_over: float | None = None) -> int:
    """Up to cfg.descriptions_per_tick fetches, and no new fetch once the tick has spent
    cfg.description_seconds_per_tick on them (the first fetch always happens). A slow LinkedIn
    pace would otherwise keep one tick inside this stage for hours and starve the cheap checks,
    merging, verification and scoring that come after it. A fetch the pace had to defer
    (refused) ends the round: more requests now would only be refused too.

    `skip_if_wait_over` is for a tick that already has other work: when the pace says the
    next LinkedIn turn is further away than that many seconds, do not queue for it.

    The schedule gate is checked first: when LinkedIn is closed (a configured closed window, the shared
    daily cap) the whole stage is skipped, so the rest of the tick's cheap
    checks, verification and scoring still run."""
    if not pace.gate().open:
        return 0
    done = 0
    started = clock()
    while done < cfg.descriptions_per_tick:
        if done and clock() - started >= cfg.description_seconds_per_tick:
            break
        if skip_if_wait_over is not None and pace.seconds_until_slot() > skip_if_wait_over:
            break
        pulse({"describing": done})
        outcome = describe_next(conn, pace, cfg, now, get=get, sleep=sleep)
        if outcome is None:
            break
        done += 1
        if outcome in ("throttled", "refused", "deferred"):
            break
    return done
