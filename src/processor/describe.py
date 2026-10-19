"""Stage 2: get the description (spec section 5, step 2).

Sources that return text inline never reach this stage. For LinkedIn jobs the
guest fragment is fetched through the shared pace. A host throttle (429/999)
returns the job to the queue unchanged except for its `rate_limited` mark, with no
per-job penalty; job-specific refusals (401/403/503/504) and the unreadable-markup
path keep the 30 minutes doubling to 12 hours backoff. The job expires after 7
days of host refusals, or when the posting is over 30 days old. The title+company
fallback is never used as a description.

The queue is not swept tier by tier: a job past `description_urgent_after_hours`
goes first whatever its tier, and otherwise the tiers take turns. See `_next_job`.
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

# The filters a description pick applies. Shared so `_next_job` and
# `oldest_waiting_description` (hence `description_overdue`) cannot drift apart.
_WAITING = ("stage = 'seen' AND tier IS NOT NULL "
            "AND description_status IN ('pending', 'rate_limited') "
            "AND next_attempt_at <= %s")
# One kv_state row per tier holding when that tier last had a description fetch
# attempt. A timestamp column would need a schema change (and its
# AccessExclusiveLock); the jobs table has no description-fetch time to derive it
# from, because stage_changed_at moves on with every later stage.
TURN_KEY = "description_turn:"


def refusal_delay(attempts: int, cfg: ProcessorConfig) -> timedelta:
    """Wait after the `attempts`-th refusal: 30 min, 1 h, 2 h ... capped at 12 h."""
    minutes = min(cfg.backoff_first_minutes * 2 ** max(attempts - 1, 0), cfg.backoff_cap_hours * 60)
    return timedelta(minutes=minutes)


def oldest_waiting_description(cur, now: datetime) -> datetime | None:
    """The `first_seen_at` of the oldest job fetchable for a description right now
    (the same `_WAITING` filters as `_next_job`), or None when none is waiting."""
    cur.execute(f"SELECT min(first_seen_at) FROM jobs WHERE {_WAITING}", (now,))
    return cur.fetchone()[0]


def description_overdue(cur, cfg: ProcessorConfig, now: datetime) -> bool:
    """True when the oldest waiting job was first seen more than
    `description_deadline_hours` ago: an overdue description may spend more than one
    processor tick waiting for its turn.

    This is the pace's guard, not the queue order: it keeps its meaning (a description
    that is late may wait out the tick budget) and stays independent of
    `description_urgent_after_hours`, which only decides who goes first."""
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


def _next_job(cur, cfg: ProcessorConfig, now: datetime) -> JobRow | None:
    """The next job to fetch a description for, in two cheap queries.

    1. A job past `description_urgent_after_hours` goes first whatever its tier,
       oldest first, so nothing can miss the digest's 4-day target behind a backlog
       in a busier tier.
    2. Otherwise the tiers take turns: the tier whose last fetch attempt is oldest
       goes first (a tier never served yet is oldest of all), then that tier's oldest
       job; ties go to tier 1. This mirrors `claim_due_slice` in
       `src/linkedin/walk.py`, so no tier can starve.

    `ORDER BY tier, first_seen_at` gave tier 3 and tier 4 a turn only once tiers 1 and 2
    were empty: on 2026-10-19 that left 936 tier-3 descriptions waiting, the oldest past
    the 4-day target. The pace and the number of requests are unaffected.

    Ties break on `note_no`, the insertion-order identity column, so the pick is stable
    for jobs recorded in the same second (the previous query got that order from the
    index scan, and a random UUID would reshuffle it on every call)."""
    cutoff = now - timedelta(hours=cfg.description_urgent_after_hours)
    cur.execute(
        f"SELECT id FROM jobs WHERE {_WAITING} AND first_seen_at < %s "
        "ORDER BY first_seen_at, tier, note_no LIMIT 1", (now, cutoff))
    row = cur.fetchone()
    if row is None:
        cur.execute(
            f"WITH waiting AS (SELECT id, tier, first_seen_at, note_no FROM jobs "
            f"WHERE {_WAITING} AND first_seen_at >= %s) "
            "SELECT w.id FROM waiting w "
            "LEFT JOIN kv_state k ON k.key = %s || w.tier "
            "ORDER BY (k.value->>'at')::timestamptz NULLS FIRST, w.tier, w.first_seen_at, "
            "w.note_no LIMIT 1", (now, cutoff, TURN_KEY))
        row = cur.fetchone()
    return load_job(cur, row[0]) if row else None


def _record_turn(cur, tier: int) -> None:
    """Remember that `tier` just had a description fetch attempt, so the next pick
    starts with the tier served longest ago. Plain upsert on one kv_state row per
    tier: no DDL, so no schema fingerprint change and nothing to migrate on deploy.

    The time is the store's `clock_timestamp()`, not the tick's `now`: every fetch in a
    tick shares one `now`, so tiers served in the same tick would tie and the lowest
    tier would win every tie, drifting back to tier order."""
    cur.execute(
        "INSERT INTO kv_state (key, value, updated_at) "
        "VALUES (%s, jsonb_build_object('at', clock_timestamp()), clock_timestamp()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at",
        (f"{TURN_KEY}{tier}",))


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
        job = _next_job(cur, cfg, now)
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
        if result.status == "deferred":
            # The schedule gate closed (a configured closed window or the daily cap) between
            # requests. Leave the job exactly as it is; the next tick retries it.
            return "deferred"
        # Any attempt is this tier's turn in the rotation, whatever the outcome: a tier
        # whose fetches keep being throttled must not keep taking the next turn.
        _record_turn(cur, job.tier)
        if result.status == "ok":
            cur.execute(
                "UPDATE jobs SET description = %s, description_status = 'ok', "
                "description_source = 'linkedin', first_refused_at = NULL, attempts = 0, "
                "posted_text = %s WHERE id = %s",
                (result.text, result.posted_text or None, job.id))
            job.description = result.text
            finish_with_description(cur, job, now)
            outcome = "described"
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
