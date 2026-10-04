import hashlib
from datetime import datetime, timezone
import types as _types

try:
    import psycopg2
except ImportError:
    psycopg2 = _types.ModuleType("psycopg2")
    psycopg2.connect = None

from src.models import ScoredOffer
from src.database import apply_transaction_timeouts


def _link_hash(link: str) -> str:
    return hashlib.md5(link.encode()).hexdigest()


def init_db(db_url: str, *, connect_timeout: int | None = None) -> None:
    if db_url is None:
        return
    try:
        kwargs = {}
        if connect_timeout is not None:
            kwargs["connect_timeout"] = connect_timeout
        conn = psycopg2.connect(db_url, **kwargs)
        if connect_timeout is not None:
            apply_transaction_timeouts(conn)
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS runs (
                        id                SERIAL PRIMARY KEY,
                        run_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        tier              INTEGER NOT NULL,
                        offers_fetched    INTEGER,
                        offers_new        INTEGER,
                        prompt_tokens     INTEGER,
                        completion_tokens INTEGER,
                        total_tokens      INTEGER
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS offers (
                        id                 SERIAL PRIMARY KEY,
                        run_id             INTEGER NOT NULL REFERENCES runs(id),
                        link               TEXT NOT NULL,
                        title              TEXT,
                        company            TEXT,
                        location           TEXT,
                        work_mode          TEXT,
                        description        TEXT,
                        description_status VARCHAR(20) NOT NULL DEFAULT 'ok',
                        score              INTEGER,
                        comment            TEXT,
                        summary            TEXT,
                        tier               INTEGER,
                        run_at             TIMESTAMPTZ,
                        remote_verdict     VARCHAR(12),
                        remote_reason      TEXT
                    )
                """)
                cur.execute("""ALTER TABLE offers ADD COLUMN IF NOT EXISTS description_status VARCHAR(20) NOT NULL DEFAULT 'ok'""")
                cur.execute("""ALTER TABLE offers ALTER COLUMN description_status TYPE VARCHAR(20)""")
                cur.execute("""ALTER TABLE offers ADD COLUMN IF NOT EXISTS application_channel VARCHAR(20)""")
                cur.execute("""ALTER TABLE offers ADD COLUMN IF NOT EXISTS remote_verdict VARCHAR(12)""")
                cur.execute("""ALTER TABLE offers ADD COLUMN IF NOT EXISTS remote_reason TEXT""")
                # --- run observability (data/run-observability/design.md) ---
                for column in (
                    "run_uuid UUID", "daily_run_id TEXT", "attempt INTEGER",
                    "git_commit TEXT", "git_dirty BOOLEAN", "status VARCHAR(10)",
                    "started_at TIMESTAMPTZ", "finished_at TIMESTAMPTZ", "error TEXT",
                    "offers_deferred INTEGER", "offers_packaged INTEGER",
                    "verification_provider TEXT", "verification_tokens INTEGER",
                    "verification_batches_failed INTEGER", "verification_batches_total INTEGER",
                    "verification_degraded BOOLEAN", "verification_confirmed INTEGER",
                    "verification_unconfirmed INTEGER", "verification_rejected INTEGER",
                    "search_rate_limits INTEGER", "description_rate_limits INTEGER",
                    "rate_limit_deferred INTEGER", "rate_limit_dropped INTEGER",
                    "telemetry_ok BOOLEAN",
                ):
                    cur.execute(f"ALTER TABLE runs ADD COLUMN IF NOT EXISTS {column}")
                # Unique so buffered run records upsert idempotently; NULL for
                # pre-telemetry rows, which Postgres allows any number of.
                cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS runs_run_uuid_key ON runs (run_uuid)")
                cur.execute("CREATE INDEX IF NOT EXISTS runs_daily_run_id_idx ON runs (daily_run_id)")
                # Child rows reference runs by run_uuid, not runs.id: they must be
                # writable from the local buffer while Neon is unreachable, before
                # any serial id exists. No foreign key, deliberately - a flush must
                # never fail on ordering; joins go through run_uuid.
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS run_queries (
                        id           UUID PRIMARY KEY,
                        run_uuid     UUID NOT NULL,
                        role         TEXT,
                        location     TEXT,
                        work_mode    TEXT,
                        pages_walked INTEGER,
                        page_cap     INTEGER,
                        cards_seen   INTEGER,
                        offers_kept  INTEGER,
                        stop_reason  VARCHAR(20) NOT NULL,
                        recorded_at  TIMESTAMPTZ NOT NULL
                    )
                """)
                # Added after the table shipped; NULL on older rows.
                cur.execute("ALTER TABLE run_queries ADD COLUMN IF NOT EXISTS cross_query_duplicates INTEGER")
                cur.execute("CREATE INDEX IF NOT EXISTS run_queries_run_uuid_idx ON run_queries (run_uuid)")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS llm_calls (
                        id             UUID PRIMARY KEY,
                        run_uuid       UUID,
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
                        started_at     TIMESTAMPTZ NOT NULL
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS llm_calls_run_uuid_idx ON llm_calls (run_uuid)")
                cur.execute("CREATE INDEX IF NOT EXISTS llm_calls_started_at_idx ON llm_calls (started_at)")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS applications (
                        id           SERIAL PRIMARY KEY,
                        link_hash    VARCHAR(32) NOT NULL UNIQUE,
                        link         TEXT NOT NULL,
                        title        TEXT,
                        company      TEXT,
                        channel      VARCHAR(20),
                        packaged_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        dry_run      BOOLEAN NOT NULL DEFAULT FALSE
                    )
                """)
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[storage] init_db failed: {e}")


_OFFER_RUN_COLUMNS = (
    "run_uuid", "run_at", "tier", "daily_run_id", "attempt",
    "git_commit", "git_dirty", "status", "started_at",
)


def _ensure_offer_run(cur, run_id: int | None, tier: int, run_data: dict | None, now) -> int:
    if run_id is not None:
        return run_id

    data = {
        "run_at": now,
        "tier": tier,
        "status": "running",
        "started_at": now,
        **(run_data or {}),
    }
    data["tier"] = tier
    placeholders = ", ".join(["%s"] * len(_OFFER_RUN_COLUMNS))
    cur.execute(
        f"INSERT INTO runs ({', '.join(_OFFER_RUN_COLUMNS)}) VALUES ({placeholders}) "
        "ON CONFLICT (run_uuid) DO UPDATE SET run_uuid = EXCLUDED.run_uuid "
        "RETURNING id",
        [data.get(column) for column in _OFFER_RUN_COLUMNS],
    )
    return cur.fetchone()[0]


def save_offers(
    db_url: str,
    offers: list[ScoredOffer],
    run_id: int | None,
    tier: int,
    *,
    run_data: dict | None = None,
) -> bool:
    """Persist scored offers. False means the rows were not written.

    Callers that ignore the return keep the old behaviour: a storage error is
    printed and does not raise. A caller that checks it can keep the run from
    looking healthy when the funnel rows never landed.
    """
    if not offers or db_url is None:
        return True
    try:
        now = datetime.now(timezone.utc)
        conn = psycopg2.connect(db_url)
        try:
            with conn.cursor() as cur:
                run_id = _ensure_offer_run(cur, run_id, tier, run_data, now)
                for offer in offers:
                    cur.execute(
                        """
                        INSERT INTO offers
                            (run_id, link, title, company, location, work_mode,
                             description, description_status, score, comment, summary, tier, run_at,
                             remote_verdict, remote_reason)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            run_id, offer.link, offer.title, offer.company, offer.location,
                            offer.work_mode, offer.description, offer.description_status,
                            offer.score, offer.comment, offer.summary, tier, now,
                            offer.remote_verdict, offer.remote_reason,
                        ),
                    )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:
        print(f"[storage] save_offers failed: {e}")
        return False


def save_application_channel(db_url: str, link: str, channel: str) -> None:
    if db_url is None:
        return
    try:
        conn = psycopg2.connect(db_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE offers SET application_channel = %s WHERE link = %s",
                    (channel, link),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[storage] save_application_channel failed: {e}")


def is_application_packaged(db_url: str, link: str) -> bool:
    """True if this offer link has already been packaged in a past run, dry-run or
    not — the application-time dedup gate, distinct from src/dedup.py's scrape-time
    dedup. Dry-run packages count too, so a still-open offer isn't re-tailored every
    day auto-apply stays in dry-run mode."""
    if db_url is None:
        return False
    try:
        conn = psycopg2.connect(db_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM applications WHERE link_hash = %s",
                    (_link_hash(link),),
                )
                return cur.fetchone() is not None
        finally:
            conn.close()
    except Exception as e:
        print(f"[storage] is_application_packaged failed: {e}")
        return False


def save_application(
    db_url: str, link: str, title: str, company: str, channel: str, dry_run: bool
) -> None:
    if db_url is None:
        return
    try:
        conn = psycopg2.connect(db_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO applications (link_hash, link, title, company, channel, dry_run)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (link_hash) DO UPDATE SET
                        channel = EXCLUDED.channel,
                        packaged_at = NOW(),
                        dry_run = EXCLUDED.dry_run
                    """,
                    (_link_hash(link), link, title, company, channel, dry_run),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[storage] save_application failed: {e}")


def count_applications_packaged_today(db_url: str) -> int:
    """Counts packages regardless of dry_run so the daily cap actually limits
    tailoring calls (and their OpenRouter quota) while dry-run stays on, not just once
    notifications go live."""
    if db_url is None:
        return 0
    try:
        conn = psycopg2.connect(db_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM applications WHERE packaged_at::date = CURRENT_DATE"
                )
                return cur.fetchone()[0]
        finally:
            conn.close()
    except Exception as e:
        print(f"[storage] count_applications_packaged_today failed: {e}")
        return 0
