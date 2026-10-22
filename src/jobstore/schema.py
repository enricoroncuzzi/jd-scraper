"""DDL for the server-side job store. Every statement is idempotent, so
init_store() can run at every service start (same pattern as src/storage.py).

Table names and columns here are the contract the collector, processor and
delivery services share. Change them only together with the plans in
data/jdscraper-pipeline-redesign/plans/.

init_store() records a fingerprint of DDL in kv_state and, when the store already
matches it, skips the DDL entirely. That matters because the additive ALTER
TABLE ... ADD COLUMN IF NOT EXISTS statements still take an AccessExclusiveLock
on the table even when the column is present, and a service start taking that
lock while a running service holds an ordinary read lock on jobs deadlocks (the
2026-10-16 incident). A fingerprint, not a hand-bumped version, because it
changes exactly when DDL changes: a new column cannot be added without
invalidating it, so an edited DDL list can never be silently skipped.
"""
import hashlib
import json

STAGES = ("seen", "described", "filtered_out", "scored", "delivered", "expired")
DESCRIPTION_STATUSES = ("ok", "pending", "rate_limited", "failed")
# The verdict vocabulary (src/remote_verdicts.py). `advised` and `not_stated`
# arrived 2026-10-18; `unconfirmed` is the legacy value old rows still carry and
# is read as not_stated everywhere.
REMOTE_VERDICTS = ("confirmed", "advised", "not_stated", "rejected", "not_checked", "unconfirmed")

DDL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS slices (
        id                    BIGSERIAL PRIMARY KEY,
        source                TEXT NOT NULL,
        role                  TEXT NOT NULL,
        area                  TEXT NOT NULL,
        tier                  SMALLINT NOT NULL,
        active                BOOLEAN NOT NULL DEFAULT TRUE,
        base_interval_seconds INTEGER NOT NULL,
        interval_seconds      INTEGER NOT NULL,
        last_walked_at        TIMESTAMPTZ,
        last_window           INTEGER,
        pages_walked          INTEGER,
        cards_seen            INTEGER,
        stop_reason           TEXT,
        consecutive_empty     INTEGER NOT NULL DEFAULT 0,
        resume_start          INTEGER NOT NULL DEFAULT 0,
        due_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (source, role, area)
    )
    """,
    "CREATE INDEX IF NOT EXISTS slices_due_idx ON slices (tier, due_at) WHERE active",
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        note_no             BIGINT GENERATED ALWAYS AS IDENTITY,
        canonical_url       TEXT NOT NULL,
        title               TEXT NOT NULL,
        company             TEXT NOT NULL,
        company_norm        TEXT NOT NULL,
        title_norm          TEXT NOT NULL,
        country             TEXT,
        location_raw        TEXT NOT NULL DEFAULT 'N/A',
        work_mode           TEXT NOT NULL DEFAULT '',
        remote_hint         TEXT,
        posted_at           TIMESTAMPTZ,
        first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
        description         TEXT NOT NULL DEFAULT '',
        description_status  TEXT NOT NULL DEFAULT 'pending',
        description_source  TEXT,
        first_refused_at    TIMESTAMPTZ,
        language            TEXT,
        tier_hint           SMALLINT,
        tier                SMALLINT,
        remote_verdict      TEXT,
        remote_reason       TEXT NOT NULL DEFAULT '',
        remote_advice       TEXT NOT NULL DEFAULT '',
        score               SMALLINT,
        summary             TEXT NOT NULL DEFAULT '',
        comment             TEXT NOT NULL DEFAULT '',
        application_channel TEXT,
        stage               TEXT NOT NULL DEFAULT 'seen',
        stage_reason        TEXT NOT NULL DEFAULT '',
        stage_changed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        attempts            INTEGER NOT NULL DEFAULT 0,
        next_attempt_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
        note_day            DATE,
        delivery_day        DATE,
        delivered_at        TIMESTAMPTZ,
        alerted_at          TIMESTAMPTZ,
        archived_at         TIMESTAMPTZ,
        CONSTRAINT jobs_stage_chk CHECK (
            stage IN ('seen','described','filtered_out','scored','delivered','expired')),
        CONSTRAINT jobs_description_status_chk CHECK (
            description_status IN ('ok','pending','rate_limited','failed')),
        CONSTRAINT jobs_remote_verdict_chk CHECK (
            remote_verdict IS NULL
            OR remote_verdict IN (
                'confirmed','advised','not_stated','rejected','not_checked','unconfirmed')),
        CONSTRAINT jobs_score_chk CHECK (score IS NULL OR score BETWEEN 1 AND 10)
    )
    """,
    "CREATE INDEX IF NOT EXISTS jobs_stage_tier_idx ON jobs (stage, tier, first_seen_at)",
    "CREATE INDEX IF NOT EXISTS jobs_identity_idx ON jobs (company_norm, title_norm, country)",
    """CREATE INDEX IF NOT EXISTS jobs_next_attempt_idx ON jobs (next_attempt_at)
       WHERE stage IN ('seen', 'described')""",
    # The description queue (src/processor/describe.py `_next_job`): the floor pick reads the
    # waiting set in first_seen_at order, the class pick reads it with the priority class
    # first. Without this they fell back to a full scan (23 ms on 200k rows, every fetch).
    # The partial predicate matches the queries exactly.
    """CREATE INDEX IF NOT EXISTS jobs_description_queue_idx ON jobs (first_seen_at, tier, note_no)
       WHERE stage = 'seen' AND tier IS NOT NULL
         AND description_status IN ('pending', 'rate_limited')""",
    """CREATE INDEX IF NOT EXISTS jobs_unarchived_idx ON jobs (stage)
       WHERE archived_at IS NULL AND stage IN ('scored', 'delivered', 'expired')""",
    # The per-tier priority rank of the description fetch order
    # (src/processor/fetch_order.py): written when the tier is assigned, read by the
    # description pick. NULL = not classified yet; the pick orders NULL last and the
    # processor's per-tick backfill fills it in. No index: the pick sorts the waiting
    # set anyway (measured in data/jdscraper-smart-fetch-order/plan.md).
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS description_class SMALLINT",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS posted_text TEXT",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS remote_advice TEXT NOT NULL DEFAULT ''",
    # 2026-10-18 verdict split: the existing constraint only allowed the four old
    # values, so `advised`/`not_stated` writes would fail on a live store. The
    # DDL fingerprint changes, so init_store runs this once on deploy. One
    # statement, because two services starting together (a rolling restart) must
    # not race a DROP and an ADD into a duplicate-constraint error.
    """DO $$
    BEGIN
        ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_remote_verdict_chk;
        ALTER TABLE jobs ADD CONSTRAINT jobs_remote_verdict_chk CHECK (
            remote_verdict IS NULL
            OR remote_verdict IN (
                'confirmed','advised','not_stated','rejected','not_checked','unconfirmed'));
    EXCEPTION WHEN duplicate_object THEN NULL;
    END $$""",
    """
    CREATE TABLE IF NOT EXISTS sightings (
        id            BIGSERIAL PRIMARY KEY,
        job_id        UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        source        TEXT NOT NULL,
        source_job_id TEXT NOT NULL,
        source_url    TEXT NOT NULL,
        source_detail TEXT,
        seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        seen_count    INTEGER NOT NULL DEFAULT 1,
        slice_id      BIGINT REFERENCES slices(id) ON DELETE SET NULL,
        archived_at   TIMESTAMPTZ,
        UNIQUE (source, source_job_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS sightings_job_idx ON sightings (job_id)",
    "CREATE INDEX IF NOT EXISTS sightings_seen_idx ON sightings (seen_at)",
    """
    CREATE TABLE IF NOT EXISTS job_applications (
        job_id      UUID PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
        channel     VARCHAR(20),
        packaged_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        dry_run     BOOLEAN NOT NULL DEFAULT FALSE,
        archived_at TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS llm_calls (
        id             UUID PRIMARY KEY,
        run_uuid       UUID,
        service        TEXT,
        stage          VARCHAR(12) NOT NULL,
        provider       VARCHAR(12) NOT NULL,
        request_model  TEXT NOT NULL,
        response_model TEXT,
        input_tokens   INTEGER,
        output_tokens  INTEGER,
        latency_ms     INTEGER NOT NULL,
        batch_size     INTEGER NOT NULL,
        attempt        INTEGER NOT NULL,
        outcome        VARCHAR(16) NOT NULL,
        error          TEXT,
        prompt_version VARCHAR(12) NOT NULL,
        started_at     TIMESTAMPTZ NOT NULL,
        archived_at    TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS llm_calls_started_at_idx ON llm_calls (started_at)",
    """
    CREATE TABLE IF NOT EXISTS llm_call_jobs (
        call_id UUID NOT NULL,
        job_id  UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        PRIMARY KEY (call_id, job_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS llm_budget (
        day           DATE NOT NULL,
        provider      TEXT NOT NULL,
        requests      INTEGER NOT NULL DEFAULT 0,
        input_tokens  BIGINT NOT NULL DEFAULT 0,
        output_tokens BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (day, provider)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pace_budgets (
        host         TEXT PRIMARY KEY,
        factor       DOUBLE PRECISION NOT NULL DEFAULT 1.0,
        clean_streak INTEGER NOT NULL DEFAULT 0,
        next_slot_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        cooldown_until TIMESTAMPTZ,
        last_kind    TEXT,
        wanting      JSONB NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    "ALTER TABLE pace_budgets ADD COLUMN IF NOT EXISTS calm_since TIMESTAMPTZ",
    # Schedule C gate state (config/schedule.json): the shared Rome-day request counter
    # and the circuit breaker. Additive ALTERs so an existing production row upgrades in place.
    "ALTER TABLE pace_budgets ADD COLUMN IF NOT EXISTS requests_today INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE pace_budgets ADD COLUMN IF NOT EXISTS day DATE",
    "ALTER TABLE pace_budgets ADD COLUMN IF NOT EXISTS refusal_times TIMESTAMPTZ[] NOT NULL DEFAULT '{}'",
    "ALTER TABLE pace_budgets ADD COLUMN IF NOT EXISTS rest_until TIMESTAMPTZ",
    "ALTER TABLE pace_budgets ADD COLUMN IF NOT EXISTS rest_level INTEGER NOT NULL DEFAULT 0",
    # Always-on pace state (design 2026-10-16). factor, clean_streak, calm_since,
    # rest_until and rest_level are no longer read.
    "ALTER TABLE pace_budgets ADD COLUMN IF NOT EXISTS refusals_today INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE pace_budgets ADD COLUMN IF NOT EXISTS refusal_streak INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE pace_budgets ADD COLUMN IF NOT EXISTS request_times TIMESTAMPTZ[] NOT NULL DEFAULT '{}'",
    "ALTER TABLE pace_budgets ADD COLUMN IF NOT EXISTS nudge DOUBLE PRECISION NOT NULL DEFAULT 1.0",
    "ALTER TABLE pace_budgets ADD COLUMN IF NOT EXISTS nudged_at TIMESTAMPTZ",
    "ALTER TABLE pace_budgets ADD COLUMN IF NOT EXISTS weekday_spacing_seconds DOUBLE PRECISION",
    "ALTER TABLE pace_budgets ADD COLUMN IF NOT EXISTS weekend_spacing_seconds DOUBLE PRECISION",
    "ALTER TABLE pace_budgets ADD COLUMN IF NOT EXISTS calm_days INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE pace_budgets ADD COLUMN IF NOT EXISTS last_clean_at TIMESTAMPTZ",
    # Fixed pace (2026-10-18 refusal study): the nudge, the stored bases and the day verdict
    # are no longer read. description_run is how many descriptions have taken the last turns
    # in a row, so the collector and processor share the two-of-three turn share.
    "ALTER TABLE pace_budgets ADD COLUMN IF NOT EXISTS description_run INTEGER NOT NULL DEFAULT 0",
    # Adaptive pace ladder (2026-10-22): one JSONB state per host, and the level each
    # request was made at, for replay and operations. Additive, so old code ignores them.
    "ALTER TABLE pace_budgets ADD COLUMN IF NOT EXISTS adaptive_state JSONB NOT NULL DEFAULT '{}'::jsonb",
    """
    CREATE TABLE IF NOT EXISTS pace_events (
        id             BIGSERIAL PRIMARY KEY,
        at             TIMESTAMPTZ NOT NULL DEFAULT now(),
        service        TEXT,
        kind           TEXT NOT NULL,
        outcome        TEXT NOT NULL,
        status         INTEGER,
        factor         DOUBLE PRECISION,
        requests_today INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS pace_events_at_idx ON pace_events (at)",
    "ALTER TABLE pace_events ADD COLUMN IF NOT EXISTS spacing_seconds DOUBLE PRECISION",
    "ALTER TABLE pace_events ADD COLUMN IF NOT EXISTS wait_seconds DOUBLE PRECISION",
    "ALTER TABLE pace_events ADD COLUMN IF NOT EXISTS level INTEGER",
    """
    CREATE TABLE IF NOT EXISTS slice_walks (
        id             BIGSERIAL PRIMARY KEY,
        slice_id       BIGINT NOT NULL REFERENCES slices(id) ON DELETE CASCADE,
        walked_at      TIMESTAMPTZ NOT NULL,
        pages          INTEGER NOT NULL,
        cards_seen     INTEGER NOT NULL,
        new_jobs       INTEGER NOT NULL,
        stop_reason    TEXT NOT NULL,
        window_seconds INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS slice_walks_slice_idx ON slice_walks (slice_id, walked_at)",
    """
    CREATE TABLE IF NOT EXISTS service_heartbeats (
        service    TEXT PRIMARY KEY,
        started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        beat_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        state      TEXT NOT NULL DEFAULT 'starting',
        detail     JSONB NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS delivery_runs (
        day          DATE NOT NULL,
        tier         SMALLINT NOT NULL,
        completed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        job_count    INTEGER NOT NULL DEFAULT 0,
        telegram_sent_at TIMESTAMPTZ,
        PRIMARY KEY (day, tier)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS legacy_seen_hashes (
        link_hash CHAR(32) PRIMARY KEY,
        tier      SMALLINT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kv_state (
        key        TEXT PRIMARY KEY,
        value      JSONB NOT NULL DEFAULT '{}'::jsonb,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
]

TABLES = (
    "slices", "jobs", "sightings", "job_applications", "llm_calls", "llm_call_jobs",
    "llm_budget", "pace_budgets", "pace_events", "slice_walks", "service_heartbeats", "delivery_runs",
    "legacy_seen_hashes", "kv_state",
)


def schema_fingerprint() -> str:
    """A stable hash of the DDL. Changes exactly when the schema definition does."""
    return hashlib.sha256("\n\n".join(DDL).encode("utf-8")).hexdigest()


SCHEMA_VERSION_KEY = "schema_version"


def _stored_schema_fingerprint(conn) -> str | None:
    """The fingerprint recorded in the store, or None when it predates the key.

    Plain reads only: `to_regclass` checks for kv_state (a fresh store has none)
    without raising, and neither it nor the SELECT takes a lock on any other
    table, so this never contends with a running service."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('kv_state')")
        if cur.fetchone()[0] is None:
            return None
        cur.execute("SELECT value FROM kv_state WHERE key = %s", (SCHEMA_VERSION_KEY,))
        row = cur.fetchone()
    if row is None or not isinstance(row[0], str):
        return None
    return row[0]


def init_store(conn) -> None:
    """Create every table and index if missing. Safe to call on every start.

    Current schema: a plain read of the stored fingerprint, then return. No DDL,
    so no AccessExclusiveLock and no deadlock with a running service.
    Stale or absent schema (a real migration): run the idempotent DDL, then
    record the fingerprint.
    """
    fingerprint = schema_fingerprint()
    if _stored_schema_fingerprint(conn) == fingerprint:
        conn.commit()
        return
    with conn.cursor() as cur:
        for statement in DDL:
            cur.execute(statement)
        cur.execute(
            "INSERT INTO kv_state (key, value, updated_at) VALUES (%s, %s::jsonb, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (SCHEMA_VERSION_KEY, json.dumps(fingerprint)),
        )
    conn.commit()
