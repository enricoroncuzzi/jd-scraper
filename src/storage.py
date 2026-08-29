import hashlib
from datetime import datetime, timezone
import types as _types

try:
    import psycopg2
except ImportError:
    psycopg2 = _types.ModuleType("psycopg2")
    psycopg2.connect = None

from src.models import ScoredOffer


def _link_hash(link: str) -> str:
    return hashlib.md5(link.encode()).hexdigest()


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
                        id                 SERIAL PRIMARY KEY,
                        run_id             INTEGER NOT NULL REFERENCES runs(id),
                        link               TEXT NOT NULL,
                        title              TEXT,
                        company            TEXT,
                        location           TEXT,
                        work_mode          TEXT,
                        description        TEXT,
                        description_status VARCHAR(10) NOT NULL DEFAULT 'ok',
                        score              INTEGER,
                        comment            TEXT,
                        summary            TEXT,
                        tier               INTEGER,
                        run_at             TIMESTAMPTZ
                    )
                """)
                cur.execute("""ALTER TABLE offers ADD COLUMN IF NOT EXISTS description_status VARCHAR(10) NOT NULL DEFAULT 'ok'""")
                cur.execute("""ALTER TABLE offers ADD COLUMN IF NOT EXISTS application_channel VARCHAR(20)""")
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
                             description, description_status, score, comment, summary, tier, run_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            run_id, offer.link, offer.title, offer.company, offer.location,
                            offer.work_mode, offer.description, offer.description_status,
                            offer.score, offer.comment, offer.summary, tier, now,
                        ),
                    )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[storage] save_offers failed: {e}")


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
    tailoring calls (and their Groq cost) while dry-run stays on, not just once
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
