from datetime import datetime, timezone
import types as _types

try:
    import psycopg2
except ImportError:
    # psycopg2 not installed — create a stub so the module can be imported
    # and tests can patch src.storage.psycopg2.connect cleanly.
    psycopg2 = _types.ModuleType("psycopg2")
    psycopg2.connect = None  # placeholder; replaced by patch() in tests

from src.models import ScoredOffer


def init_db(db_url: str) -> None:
    if db_url is None:
        return
    try:
        conn = psycopg2.connect(db_url)
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
                        id          SERIAL PRIMARY KEY,
                        run_id      INTEGER NOT NULL REFERENCES runs(id),
                        link        TEXT NOT NULL,
                        title       TEXT,
                        company     TEXT,
                        location    TEXT,
                        work_mode   TEXT,
                        description TEXT,
                        score       INTEGER,
                        comment     TEXT,
                        summary     TEXT,
                        tier        INTEGER,
                        run_at      TIMESTAMPTZ
                    )
                """)
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[storage] init_db failed: {e}")


def save_run(
    db_url: str,
    tier: int,
    offers_fetched: int,
    offers_new: int,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
) -> int:
    if db_url is None:
        return 0
    try:
        conn = psycopg2.connect(db_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO runs (tier, offers_fetched, offers_new, prompt_tokens, completion_tokens, total_tokens)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (tier, offers_fetched, offers_new, prompt_tokens, completion_tokens, total_tokens),
                )
                run_id = cur.fetchone()[0]
            conn.commit()
            return run_id
        finally:
            conn.close()
    except Exception as e:
        print(f"[storage] save_run failed: {e}")
        return 0


def save_offers(db_url: str, offers: list[ScoredOffer], run_id: int, tier: int) -> None:
    if not offers:
        return
    if db_url is None:
        return
    try:
        now = datetime.now(timezone.utc)
        conn = psycopg2.connect(db_url)
        try:
            with conn.cursor() as cur:
                for offer in offers:
                    cur.execute(
                        """
                        INSERT INTO offers
                            (run_id, link, title, company, location, work_mode,
                             description, score, comment, summary, tier, run_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            run_id, offer.link, offer.title, offer.company, offer.location,
                            offer.work_mode, offer.description, offer.score, offer.comment,
                            offer.summary, tier, now,
                        ),
                    )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[storage] save_offers failed: {e}")
