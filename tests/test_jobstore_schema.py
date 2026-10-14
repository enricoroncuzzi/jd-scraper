import psycopg2
import pytest

from src.jobstore.schema import TABLES, init_store


def _tables(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")
        return {row[0] for row in cur.fetchall()}


def _columns(conn, table):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s", (table,))
        return {row[0] for row in cur.fetchall()}


def test_init_store_creates_every_table(store_conn):
    assert set(TABLES) <= _tables(store_conn)


def test_init_store_is_idempotent(store_conn):
    init_store(store_conn)
    init_store(store_conn)
    assert set(TABLES) <= _tables(store_conn)


def test_jobs_has_the_spec_columns(store_conn):
    expected = {
        "id", "canonical_url", "title", "company", "company_norm", "title_norm", "country",
        "location_raw", "work_mode", "posted_at", "first_seen_at", "last_seen_at",
        "description", "description_status", "description_source", "language", "tier",
        "remote_verdict", "remote_reason", "score", "summary", "comment",
        "application_channel", "stage", "stage_reason", "attempts", "next_attempt_at",
        "delivered_at", "alerted_at", "archived_at",
    }
    assert expected <= _columns(store_conn, "jobs")


def test_sightings_and_slices_have_the_spec_columns(store_conn):
    assert {"job_id", "source", "source_job_id", "source_url", "seen_at", "slice_id"} \
        <= _columns(store_conn, "sightings")
    assert {"id", "source", "role", "area", "tier", "last_walked_at", "last_window",
            "pages_walked", "cards_seen", "stop_reason", "due_at"} <= _columns(store_conn, "slices")


def _insert_job(conn, **overrides):
    values = {"canonical_url": "https://example.com/1", "title": "AI Engineer",
              "company": "Acme", "company_norm": "acme", "title_norm": "ai engineer"}
    values.update(overrides)
    columns = ", ".join(values)
    placeholders = ", ".join(["%s"] * len(values))
    with conn.cursor() as cur:
        cur.execute(f"INSERT INTO jobs ({columns}) VALUES ({placeholders}) RETURNING id",
                    list(values.values()))
        return cur.fetchone()[0]


@pytest.mark.parametrize("overrides", [
    {"stage": "bogus"},
    {"description_status": "partial"},
    {"remote_verdict": "maybe"},
    {"score": 11},
    {"score": 0},
])
def test_jobs_check_constraints_reject_bad_values(store_conn, overrides):
    with pytest.raises(psycopg2.errors.CheckViolation):
        _insert_job(store_conn, **overrides)


def test_sightings_are_unique_per_source_and_source_job_id(store_conn):
    job_id = _insert_job(store_conn)
    with store_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sightings (job_id, source, source_job_id, source_url) "
            "VALUES (%s, 'linkedin', '123', 'https://x/1')", (job_id,))
        with pytest.raises(psycopg2.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO sightings (job_id, source, source_job_id, source_url) "
                "VALUES (%s, 'linkedin', '123', 'https://x/2')", (job_id,))


def test_note_no_is_a_distinct_integer_per_job(store_conn):
    first = _insert_job(store_conn, canonical_url="https://example.com/a")
    second = _insert_job(store_conn, canonical_url="https://example.com/b")
    with store_conn.cursor() as cur:
        cur.execute("SELECT note_no FROM jobs WHERE id IN (%s, %s) ORDER BY note_no", (first, second))
        numbers = [row[0] for row in cur.fetchall()]
    assert len(numbers) == 2 and numbers[0] < numbers[1]
