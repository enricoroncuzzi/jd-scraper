"""Stage 2: get the description (spec section 5, step 2).

Sources that return text inline never reach this stage. For LinkedIn jobs the
guest fragment is fetched through the shared pace. A host throttle (429/999)
returns the job to the queue unchanged except for its `rate_limited` mark, with no
per-job penalty; job-specific refusals (401/403/503/504) and the unreadable-markup
path keep the 30 minutes doubling to 12 hours backoff. The job expires after 7
days of host refusals, when the posting is over 30 days old, or when it has waited
`age_out.hours` (96) for a description in tiers 3 and 4 as a non-AI posting, which marks it
`expired` with reason `aged_out` (nothing is deleted, and `age_out.enabled` turns the rule
off). The title+company fallback is never used as a description.

The queue order is `_next_job`'s: a **floor pick** (one pick in
`quota_one_in`, decided by the head tier's own counter) takes a job past
`description_urgent_after_hours` first, whatever its tier and class, else the head
tier's oldest important job; every other pick is a **value pick** that orders the whole
queue by the measured class yield (`src/processor/fetch_order.py`) and then by age. The
pick affects only the order: nothing is dropped, skipped or expired early, and the pace
and the number of requests are unaffected.
"""
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import requests

from src.linkedin.description import fetch_description
from src.processor.cheap import finish_with_description
from src.processor.config import ProcessorConfig
from src.processor.fetch_order import UNRANKED, FetchOrder
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
# One kv_state row per tier holding how many picks that tier has made. It is per
# tier so the floor share cannot stay aligned to one tier: with a single counter
# and four waiting tiers, every quota pick landed on the same tier.
PICKS_KEY = "description_pick_count:"

# The tier whose last fetch attempt is oldest (a tier never served is oldest of
# all). It decides the kind of the pick, so its counter is per tier.
_HEAD_TIER_SQL = ("WITH waiting AS (SELECT tier, first_seen_at FROM jobs WHERE " + _WAITING + ") "
                  "SELECT w.tier FROM waiting w LEFT JOIN kv_state k ON k.key = %s || w.tier "
                  "ORDER BY (k.value->>'at')::timestamptz NULLS FIRST, w.tier LIMIT 1")
_PICKS_SQL = "SELECT coalesce((value->>'n')::int, 0) FROM kv_state WHERE key = %s"
# The floor pick: exactly the two queries this stage ran before the priority order.
_URGENT_SQL = ("SELECT id FROM jobs WHERE " + _WAITING + " AND first_seen_at < %s "
               "ORDER BY first_seen_at, tier, note_no LIMIT 1")
# The floor pick of the head tier: its oldest important job (an AI-title class), or -
# with the order disabled - its oldest job whatever its class. A NULL array keeps the
# disabled behaviour, which is exactly the order before this feature existed.
_FLOOR_SQL = ("SELECT id FROM jobs WHERE " + _WAITING + " AND tier = %s "
              "AND (%s::smallint[] IS NULL OR description_class = ANY(%s::smallint[])) "
              "ORDER BY first_seen_at, note_no LIMIT 1")
# The value pick: one global order on the measured class yield, then the oldest job.
# The CASE maps a stored (tier, class) pair to its global value rank; unclassified
# rows and ranks no class covers fall through to the ELSE and sort last.
_VALUE_SQL = ("SELECT id FROM jobs WHERE " + _WAITING + " "
              "ORDER BY {rank}, first_seen_at, note_no LIMIT 1")


def value_rank_case(order: FetchOrder) -> tuple[str, tuple]:
    """The SQL sort expression that ranks a waiting job by its global value rank.

    Returns the `CASE ... END` fragment and its bound parameters. Tiers 1 to 4 are
    enumerated because the store has no other tiers, so the default class order can be
    expanded without a join.
    """
    clauses, params = [], []
    for tier in (1, 2, 3, 4):
        for stored, cls in enumerate(order.order_for(tier).classes):
            clauses.append("WHEN tier = %s AND description_class = %s THEN %s")
            params.extend((tier, stored, order.value_rank[cls.name]))
    sql = "CASE " + " ".join(clauses) + f" ELSE {UNRANKED} END"
    return sql, tuple(params)


@dataclass(frozen=True)
class Pick:
    """The next job to fetch, why it was chosen, and whose turn decided that."""
    job: JobRow
    kind: str      # "quota" (the floor pick) or "value" (the value rank)
    tier: int      # the head tier whose pick counter decided the kind


def refusal_delay(attempts: int, cfg: ProcessorConfig) -> timedelta:
    """Wait after the `attempts`-th refusal: 30 min, 1 h, 2 h ... capped at 12 h."""
    minutes = min(cfg.backoff_first_minutes * 2 ** max(attempts - 1, 0), cfg.backoff_cap_hours * 60)
    return timedelta(minutes=minutes)


def oldest_waiting_description(cur, now: datetime) -> datetime | None:
    """The `first_seen_at` of the oldest job fetchable for a description right now
    (the same `_WAITING` filters as `_next_job`), or None when none is waiting."""
    cur.execute(f"SELECT min(first_seen_at) FROM jobs WHERE {_WAITING}", (now,))
    return cur.fetchone()[0]


def classify_waiting(conn, cfg: ProcessorConfig, now: datetime,
                     limit: int | None = None) -> int:
    """Give a priority class to waiting jobs that have none yet. Returns how many.

    A row is unclassified when it predates the deploy or when a config change
    cleared its rank. The processor classifies them in bounded batches each tick,
    using the same classifier `check_one` uses when it assigns the tier, so a
    backlog never needs a deploy script. A `limit` of None means
    `cfg.fetch_order.backfill_per_tick`.
    """
    limit = cfg.fetch_order.backfill_per_tick if limit is None else limit
    with conn.cursor() as cur:
        cur.execute(f"SELECT id, title, location_raw, tier FROM jobs WHERE {_WAITING} "
                    "AND description_class IS NULL ORDER BY first_seen_at LIMIT %s", (now, limit))
        rows = cur.fetchall()
        for job_id, title, location_raw, tier in rows:
            cur.execute("UPDATE jobs SET description_class = %s WHERE id = %s",
                        (cfg.fetch_order.classify(tier, title, location_raw), job_id))
    conn.commit()
    return len(rows)


def description_overdue(cur, cfg: ProcessorConfig, now: datetime) -> bool:
    """True when the oldest waiting job was first seen more than
    `description_deadline_hours` ago: an overdue description may spend more than one
    processor tick waiting for its turn.

    This is the pace's guard, not the queue order: it keeps its meaning (a description
    that is late may wait out the tick budget) and stays independent of
    `description_urgent_after_hours`, which only decides the floor picks."""
    oldest = oldest_waiting_description(cur, now)
    return oldest is not None and now - oldest > timedelta(hours=cfg.description_deadline_hours)


def waiting_by_class(cur, now: datetime) -> list[tuple[int, int | None, int, datetime | None]]:
    """The waiting description queue per tier and priority class.

    Rows are `(tier, class rank or None, count, oldest first_seen_at)`, for the
    status view. The `_WAITING` filter is shared with the pick, so the view cannot
    drift from what the stage will actually fetch.
    """
    cur.execute(f"SELECT tier, description_class, count(*), min(first_seen_at) FROM jobs "
                f"WHERE {_WAITING} GROUP BY 1, 2 ORDER BY 1, 2", (now,))
    return cur.fetchall()


def age_out_where(cfg: ProcessorConfig) -> tuple[str, tuple]:
    """The aged-out predicate, shared by the rule and the CLI so they cannot drift.

    Only a job *still waiting* for its description qualifies: a `seen` job whose text has
    already landed is advanced by the next cheap check, and the age-out runs before it.
    """
    return ("stage = 'seen' AND tier = ANY(%s::smallint[]) AND description_class >= %s "
            "AND description_status IN ('pending', 'rate_limited')",
            (list(cfg.age_out_tiers), cfg.age_out_min_class))


def expire_stale(conn, cfg: ProcessorConfig, now: datetime) -> int:
    """Expire jobs that waited too long. Returns how many.

    The 96 h age-out is a fourth statement, appended last so the more specific reasons
    (`description_refused`, `posting_too_old`, `waited_too_long`) keep winning on a job
    that matches two. It writes only `stage`, `stage_reason` and `stage_changed_at`, so
    nothing is lost and `python -m src.processor.age_out --undo` can restore it.
    """
    statements = [
        ("description_refused",
         "stage = 'seen' AND description_status = 'rate_limited' AND first_refused_at < %s",
         (now - timedelta(days=cfg.refusal_expiry_days),)),
        ("posting_too_old",
         "stage IN ('seen', 'described') AND posted_at IS NOT NULL AND posted_at < %s",
         (now - timedelta(days=cfg.posting_max_age_days),)),
        ("waited_too_long",
         "stage IN ('seen', 'described') AND first_seen_at < %s",
         (now - timedelta(days=cfg.posting_max_age_days),)),
    ]
    if cfg.age_out_enabled:
        where, params = age_out_where(cfg)
        statements.append(("aged_out", where + " AND first_seen_at < %s",
                           (*params, now - timedelta(hours=cfg.age_out_hours))))
    total = 0
    with conn.cursor() as cur:
        for reason, condition, params in statements:
            cur.execute(
                f"UPDATE jobs SET stage = 'expired', stage_reason = %s, stage_changed_at = %s "
                f"WHERE {condition}", (reason, now, *params))
            total += cur.rowcount
    return total


def _head_tier(cur, now: datetime) -> int | None:
    """The tier whose turn it is, or None when nothing is waiting for a description."""
    cur.execute(_HEAD_TIER_SQL, (now, TURN_KEY))
    row = cur.fetchone()
    return row[0] if row else None


def _picks_done(cur, tier: int) -> int:
    """How many picks `tier` has made; a tier with no row yet starts at 0 (a floor pick)."""
    cur.execute(_PICKS_SQL, (f"{PICKS_KEY}{tier}",))
    row = cur.fetchone()
    return row[0] if row else 0


def _next_job(cur, cfg: ProcessorConfig, now: datetime) -> Pick | None:
    """The next job to fetch a description for, by kind, with the head tier's turn.

    The head tier (the one whose last fetch attempt is oldest, ties to the lowest tier)
    decides the kind from its own counter:

    * a **floor pick** (one in `cfg.fetch_order.quota_one_in`) is the fairness pick: a job
      past `description_urgent_after_hours` goes first whatever its tier and class, oldest
      first, else the head tier's oldest important job (an AI-title class), else the value
      order. This is the 60-hour rule as a guaranteed share of the fetches, not as absolute
      priority.
    * a **value pick** orders the whole queue by the measured class yield across tiers
      (`fetch_order.value_rank`), then by age, so the best class never waits behind another
      tier's worst.

    Ties break on `note_no` so the pick is stable for jobs recorded in the same second. With
    the order disabled every pick is a floor pick with no class preference, i.e. exactly the
    behaviour before this order existed.
    """
    head = _head_tier(cur, now)
    if head is None:
        return None
    kind = "quota" if cfg.fetch_order.quota_turn(_picks_done(cur, head)) else "value"
    row = None
    if kind == "quota":
        cutoff = now - timedelta(hours=cfg.description_urgent_after_hours)
        cur.execute(_URGENT_SQL, (now, cutoff))
        row = cur.fetchone()
        if row is None:
            ranks = (list(cfg.fetch_order.important_ranks(head))
                     if cfg.fetch_order.enabled else None)
            cur.execute(_FLOOR_SQL, (now, head, ranks, ranks))
            row = cur.fetchone()
    if row is None and cfg.fetch_order.enabled:
        rank_sql, rank_params = value_rank_case(cfg.fetch_order)
        cur.execute(_VALUE_SQL.format(rank=rank_sql), (now, *rank_params))
        row = cur.fetchone()
    if row is None:
        return None
    job = load_job(cur, row[0])
    return Pick(job, kind, head) if job is not None else None


def _record_turn(cur, tier: int) -> None:
    """Remember that `tier` just had a description fetch attempt, so the next pick
    starts with the tier served longest ago. Plain upsert on one kv_state row per
    tier: no new column, so no table rewrite or AccessExclusiveLock to take.

    The time is the store's `clock_timestamp()`, not the tick's `now`: every fetch in a
    tick shares one `now`, so tiers served in the same tick would tie and the lowest
    tier would win every tie, drifting back to tier order."""
    cur.execute(
        "INSERT INTO kv_state (key, value, updated_at) "
        "VALUES (%s, jsonb_build_object('at', clock_timestamp()), clock_timestamp()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at",
        (f"{TURN_KEY}{tier}",))


def _record_pick(cur, tier: int) -> None:
    """Count a pick against `tier`'s floor share. One `kv_state` row per tier, plain
    upsert: no new column, and deleting the rows only resets the share's phase."""
    cur.execute(
        "INSERT INTO kv_state (key, value, updated_at) VALUES (%s, jsonb_build_object('n', 1), "
        "clock_timestamp()) ON CONFLICT (key) DO UPDATE SET value = jsonb_build_object('n', "
        "coalesce((kv_state.value->>'n')::int, 0) + 1), updated_at = clock_timestamp()",
        (f"{PICKS_KEY}{tier}",))


def pick_queries(cfg: ProcessorConfig, now: datetime) -> list[tuple[str, str, tuple]]:
    """Every SQL statement a description pick can run: (label, sql, bound parameters).

    For the EXPLAIN test in `tests/test_processor_describe.py`, so the queries that
    are measured are the ones the pick really runs.
    """
    cutoff = now - timedelta(hours=cfg.description_urgent_after_hours)
    ranks = list(cfg.fetch_order.important_ranks(1))
    rank_sql, rank_params = value_rank_case(cfg.fetch_order)
    return [("head", _HEAD_TIER_SQL, (now, TURN_KEY)),
            ("urgent", _URGENT_SQL, (now, cutoff)),
            ("floor", _FLOOR_SQL, (now, 1, ranks, ranks)),
            ("value", _VALUE_SQL.format(rank=rank_sql), (now, *rank_params))]


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
        pick = _next_job(cur, cfg, now)
        if pick is None:
            return None
        job = pick.job
        overdue = description_overdue(cur, cfg, now)
        linkedin_id = _linkedin_id(cur, job.id)
        if linkedin_id is None:
            set_stage(cur, job.id, "expired", "no_description_source", now)
            conn.commit()
            return "gone"
    result = fetch_description(pace, linkedin_id, overdue=overdue, get=get, sleep=sleep)
    with conn.cursor() as cur:
        if result.status == "deferred":
            # The schedule gate closed (a configured closed window) between
            # requests. Leave the job exactly as it is; the next tick retries it.
            return "deferred"
        # Any attempt is this tier's turn in the rotation, whatever the outcome: a tier
        # whose fetches keep being throttled must not keep taking the next turn. The
        # floor share counts against the tier whose turn decided the kind.
        _record_turn(cur, job.tier)
        if pick.kind == "quota" and pick.tier != job.tier:
            # A floor pick that fell through to the value order served another tier.
            # Stamp the head too, or a tier with nothing important waiting stays head
            # for ever (a never-served tier sorts first) and every other tier's floor
            # starves.
            _record_turn(cur, pick.tier)
        _record_pick(cur, pick.tier)
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

    The schedule gate is checked first: when LinkedIn is closed (a configured closed window)
    the whole stage is skipped, so the rest of the tick's cheap
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
