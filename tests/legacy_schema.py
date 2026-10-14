"""The legacy Neon tables the migration reads, created in a test schema.

Mirrors the DDL in src/storage.py::init_db and OFFER_LABELS_DDL (only the
columns the migration touches matter, but the names and types are the real ones).
"""
from datetime import datetime, timezone

LEGACY_DDL = [
    """CREATE TABLE runs (
        id SERIAL PRIMARY KEY, run_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), tier INTEGER NOT NULL)""",
    """CREATE TABLE offers (
        id SERIAL PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id), link TEXT NOT NULL,
        title TEXT, company TEXT, location TEXT, work_mode TEXT, description TEXT,
        description_status VARCHAR(20) NOT NULL DEFAULT 'ok', score INTEGER, comment TEXT,
        summary TEXT, tier INTEGER, run_at TIMESTAMPTZ, remote_verdict VARCHAR(12),
        remote_reason TEXT, application_channel VARCHAR(20))""",
    """CREATE TABLE applications (
        id SERIAL PRIMARY KEY, link_hash VARCHAR(32) NOT NULL UNIQUE, link TEXT NOT NULL,
        title TEXT, company TEXT, channel VARCHAR(20),
        packaged_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), dry_run BOOLEAN NOT NULL DEFAULT FALSE)""",
    """CREATE TABLE offer_labels (
        id SERIAL PRIMARY KEY, posting_key TEXT NOT NULL, link TEXT NOT NULL,
        label TEXT NOT NULL CHECK (label IN ('golden', 'not_for_me')),
        offer_id INTEGER REFERENCES offers(id), note TEXT,
        labeled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), UNIQUE (posting_key, label))""",
]


def create_legacy_tables(conn) -> None:
    with conn.cursor() as cur:
        for statement in LEGACY_DDL:
            cur.execute(statement)
    conn.commit()


def add_run(conn, tier: int, when: datetime) -> int:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO runs (run_at, tier) VALUES (%s, %s) RETURNING id", (when, tier))
        return cur.fetchone()[0]


def add_offer(conn, run_id: int, link: str, *, when: datetime, title="AI Engineer", company="Acme",
              location="Milan, Italy", description="Build RAG pipelines.", status="ok", score=7,
              tier=1, verdict="confirmed", channel=None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO offers (run_id, link, title, company, location, work_mode, description,
                   description_status, score, comment, summary, tier, run_at, remote_verdict,
                   remote_reason, application_channel)
               VALUES (%s, %s, %s, %s, %s, 'remote', %s, %s, %s, 'ok', 'sum', %s, %s, %s, 'why', %s)
               RETURNING id""",
            (run_id, link, title, company, location, description, status, score, tier, when,
             verdict, channel))
        return cur.fetchone()[0]


UTC = timezone.utc
