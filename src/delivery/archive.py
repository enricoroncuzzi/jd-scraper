"""Copy finished jobs from the server store to Neon, the archive (spec section 3.5).

Neon receives jobs that reached scored, delivered or expired, with their
sightings, applications and language-model call history. Every job keeps the same
UUID in both databases. The copy is idempotent (upserts) and the server store
stays the working source: if Neon is unreachable the copy fails quietly and runs
again at the next interval, and nothing else stops.

The legacy Neon tables (offers, runs, run_queries, applications) are never touched.
`offer_labels` is Neon-native (the Mac writes it); it only gains a nullable job_id,
filled in here by posting key.
"""
from datetime import datetime

from psycopg2.extras import execute_values

from src.labels import posting_key

ARCHIVE_STAGES = ("scored", "delivered", "expired")

NEON_ARCHIVE_DDL = [
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id UUID PRIMARY KEY, note_no BIGINT, canonical_url TEXT NOT NULL, title TEXT NOT NULL,
        company TEXT NOT NULL, company_norm TEXT NOT NULL, title_norm TEXT NOT NULL, country TEXT,
        location_raw TEXT NOT NULL, work_mode TEXT NOT NULL, remote_hint TEXT, posted_at TIMESTAMPTZ,
        first_seen_at TIMESTAMPTZ NOT NULL, last_seen_at TIMESTAMPTZ NOT NULL, description TEXT NOT NULL,
        description_status TEXT NOT NULL, description_source TEXT, first_refused_at TIMESTAMPTZ,
        language TEXT, tier_hint SMALLINT, tier SMALLINT, remote_verdict TEXT, remote_reason TEXT NOT NULL,
        score SMALLINT, summary TEXT NOT NULL, comment TEXT NOT NULL, application_channel TEXT,
        stage TEXT NOT NULL, stage_reason TEXT NOT NULL, stage_changed_at TIMESTAMPTZ NOT NULL,
        attempts INTEGER NOT NULL, next_attempt_at TIMESTAMPTZ NOT NULL, note_day DATE, delivery_day DATE,
        delivered_at TIMESTAMPTZ, alerted_at TIMESTAMPTZ, archived_at TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sightings (
        id BIGINT PRIMARY KEY, job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        source TEXT NOT NULL, source_job_id TEXT NOT NULL, source_url TEXT NOT NULL,
        source_detail TEXT, seen_at TIMESTAMPTZ NOT NULL, last_seen_at TIMESTAMPTZ NOT NULL, seen_count INTEGER NOT NULL,
        slice_id BIGINT, archived_at TIMESTAMPTZ, UNIQUE (source, source_job_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job_applications (
        job_id UUID PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE, channel VARCHAR(20),
        packaged_at TIMESTAMPTZ NOT NULL, dry_run BOOLEAN NOT NULL, archived_at TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS llm_calls (
        id UUID PRIMARY KEY, run_uuid UUID, stage VARCHAR(12) NOT NULL, provider VARCHAR(12) NOT NULL,
        request_model TEXT NOT NULL, response_model TEXT, input_tokens INTEGER, output_tokens INTEGER,
        latency_ms INTEGER NOT NULL, batch_size INTEGER NOT NULL, attempt INTEGER NOT NULL,
        outcome VARCHAR(16) NOT NULL, error TEXT, prompt_version VARCHAR(12) NOT NULL,
        started_at TIMESTAMPTZ NOT NULL
    )
    """,
    "ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS service TEXT",
    """
    CREATE TABLE IF NOT EXISTS llm_call_jobs (
        call_id UUID NOT NULL, job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        PRIMARY KEY (call_id, job_id)
    )
    """,
    "ALTER TABLE offer_labels ADD COLUMN IF NOT EXISTS job_id UUID",
]

JOB_COLUMNS = (
    "id", "note_no", "canonical_url", "title", "company", "company_norm", "title_norm", "country",
    "location_raw", "work_mode", "remote_hint", "posted_at", "first_seen_at", "last_seen_at",
    "description", "description_status", "description_source", "first_refused_at", "language",
    "tier_hint", "tier", "remote_verdict", "remote_reason", "score", "summary", "comment",
    "application_channel", "stage", "stage_reason", "stage_changed_at", "attempts", "next_attempt_at",
    "note_day", "delivery_day", "delivered_at", "alerted_at",
)
SIGHTING_COLUMNS = ("id", "job_id", "source", "source_job_id", "source_url", "source_detail", "seen_at",
                    "last_seen_at", "seen_count", "slice_id")
APPLICATION_COLUMNS = ("job_id", "channel", "packaged_at", "dry_run")
LLM_CALL_COLUMNS = ("id", "run_uuid", "service", "stage", "provider", "request_model", "response_model",
                    "input_tokens", "output_tokens", "latency_ms", "batch_size", "attempt", "outcome",
                    "error", "prompt_version", "started_at")


def init_archive(neon_conn) -> None:
    with neon_conn.cursor() as cur:
        for statement in NEON_ARCHIVE_DDL:
            cur.execute(statement)
    neon_conn.commit()


def _upsert(neon_cur, table: str, columns: tuple, key: str, rows: list[tuple]) -> None:
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns if c != key)
    execute_values(
        neon_cur,
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES %s "
        f"ON CONFLICT ({key}) DO UPDATE SET {updates}", rows)


def copy_jobs(store_conn, neon_conn, now: datetime, batch_size: int = 200) -> int:
    """Copy one batch of unarchived finished jobs with their sightings, application
    and call links. Returns how many jobs were copied (0 means caught up)."""
    with store_conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(JOB_COLUMNS)} FROM jobs WHERE archived_at IS NULL "
            "AND stage = ANY(%s) ORDER BY first_seen_at LIMIT %s FOR UPDATE",
            (list(ARCHIVE_STAGES), batch_size))
        jobs = cur.fetchall()
        if not jobs:
            store_conn.rollback()
            return 0
        ids = [row[0] for row in jobs]
        cur.execute(f"SELECT {', '.join(SIGHTING_COLUMNS)} FROM sightings WHERE job_id = ANY(%s::uuid[])", (ids,))
        sightings = cur.fetchall()
        cur.execute(f"SELECT {', '.join(APPLICATION_COLUMNS)} FROM job_applications "
                    "WHERE job_id = ANY(%s::uuid[])", (ids,))
        applications = cur.fetchall()
        cur.execute("SELECT call_id, job_id FROM llm_call_jobs WHERE job_id = ANY(%s::uuid[])", (ids,))
        links = cur.fetchall()
        call_ids = sorted({call_id for call_id, _ in links})
        cur.execute(f"SELECT {', '.join(LLM_CALL_COLUMNS)} FROM llm_calls WHERE id = ANY(%s::uuid[])", (call_ids,))
        calls = cur.fetchall()
    try:
        with neon_conn.cursor() as ncur:
            _upsert(ncur, "jobs", JOB_COLUMNS, "id", jobs)
            ncur.execute("DELETE FROM sightings WHERE job_id = ANY(%s::uuid[]) AND id <> ALL(%s)",
                         (ids, [row[0] for row in sightings] or [0]))
            if sightings:
                _upsert(ncur, "sightings", SIGHTING_COLUMNS, "id", sightings)
            if applications:
                _upsert(ncur, "job_applications", APPLICATION_COLUMNS, "job_id", applications)
            if calls:
                _upsert(ncur, "llm_calls", LLM_CALL_COLUMNS, "id", calls)
            if links:
                execute_values(ncur, "INSERT INTO llm_call_jobs (call_id, job_id) VALUES %s "
                                     "ON CONFLICT DO NOTHING", links)
        neon_conn.commit()
    except Exception:
        neon_conn.rollback()
        store_conn.rollback()
        raise
    with store_conn.cursor() as cur:
        cur.execute("UPDATE jobs SET archived_at = %s WHERE id = ANY(%s::uuid[])", (now, ids))
    store_conn.commit()
    return len(ids)


def backfill_label_job_ids(neon_conn) -> int:
    """Give each label without a job_id the archived job it is about, matched by
    posting key (the LinkedIn job id, or host+path for other links)."""
    with neon_conn.cursor() as cur:
        cur.execute("SELECT id, link FROM offer_labels WHERE job_id IS NULL")
        pending = cur.fetchall()
        if not pending:
            return 0
        cur.execute("SELECT job_id, source_url FROM sightings UNION SELECT id, canonical_url FROM jobs")
        by_key = {posting_key(url): job_id for job_id, url in cur.fetchall()}
        updated = 0
        for label_id, link in pending:
            job_id = by_key.get(posting_key(link))
            if job_id is not None:
                cur.execute("UPDATE offer_labels SET job_id = %s WHERE id = %s", (job_id, label_id))
                updated += 1
    neon_conn.commit()
    return updated


def archive_once(store_conn, connect_neon, now: datetime, batch_size: int = 200,
                 max_batches: int = 20) -> dict:
    """Run the copy. Never raises: a Neon outage is reported and retried later."""
    result = {"jobs": 0, "labels": 0, "error": None}
    neon = None
    try:
        neon = connect_neon()
        init_archive(neon)
        for _ in range(max_batches):
            copied = copy_jobs(store_conn, neon, now, batch_size)
            result["jobs"] += copied
            if copied < batch_size:
                break
        result["labels"] = backfill_label_job_ids(neon)
    except Exception as exc:  # noqa: BLE001 - archive trouble must never stop the services
        result["error"] = f"{type(exc).__name__}: {exc}"
        print(f"[deliver] archive copy failed ({result['error']}); will retry")
    finally:
        if neon is not None:
            neon.close()
    return result


def main(argv: list[str] | None = None) -> int:
    """python -m src.delivery.archive --init   create the archive tables in Neon (idempotent)
       python -m src.delivery.archive --once   run one copy now"""
    import argparse
    import os

    from dotenv import load_dotenv

    from src.jobstore.db import connect, store_url, utcnow

    parser = argparse.ArgumentParser(description="Neon archive")
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    load_dotenv()
    import psycopg2
    neon_url = os.environ["DATABASE_URL"]
    if args.init:
        neon = psycopg2.connect(neon_url)
        try:
            init_archive(neon)
        finally:
            neon.close()
        print("Neon archive tables are in place.")
    if args.once:
        store = connect(store_url())
        try:
            print(archive_once(store, lambda: psycopg2.connect(neon_url), utcnow()))
        finally:
            store.close()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
