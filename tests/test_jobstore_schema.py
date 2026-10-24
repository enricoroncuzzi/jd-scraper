import threading
import time

import psycopg2
import pytest

from src.jobstore.schema import TABLES, init_store, schema_fingerprint


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


def test_init_store_records_the_schema_fingerprint(store_conn):
    with store_conn.cursor() as cur:
        cur.execute("SELECT value FROM kv_state WHERE key = 'schema_version'")
        assert cur.fetchone()[0] == schema_fingerprint()


def test_the_description_queue_index_exists_and_is_partial(store_conn):
    """`_next_job` reads the waiting set on every fetch (the floor pick in
    first_seen_at order, the class pick with the priority class first), so the index
    has to cover exactly that predicate or the pick becomes a full table scan."""
    with store_conn.cursor() as cur:
        cur.execute("SELECT indexdef FROM pg_indexes WHERE indexname = 'jobs_description_queue_idx'")
        definition = cur.fetchone()[0]
    assert "first_seen_at, tier, note_no" in definition
    assert "WHERE" in definition and "description_status" in definition


def test_the_description_class_column_exists(store_conn):
    assert "description_class" in _columns(store_conn, "jobs")


@pytest.mark.parametrize("invalidate", [
    "UPDATE kv_state SET value = to_jsonb('stale'::text) WHERE key = 'schema_version'",
    "DELETE FROM kv_state WHERE key = 'schema_version'",
])
def test_init_store_reapplies_the_ddl_when_the_fingerprint_is_not_current(store_conn, invalidate):
    """A real first-time migration (new column on deploy) must still apply."""
    with store_conn.cursor() as cur:
        cur.execute("ALTER TABLE jobs DROP COLUMN posted_text")
        cur.execute(invalidate)
    store_conn.commit()
    assert "posted_text" not in _columns(store_conn, "jobs")

    init_store(store_conn)

    assert "posted_text" in _columns(store_conn, "jobs")
    with store_conn.cursor() as cur:
        cur.execute("SELECT value FROM kv_state WHERE key = 'schema_version'")
        assert cur.fetchone()[0] == schema_fingerprint()


def test_the_upgrade_to_source_requests_keeps_every_pace_events_row(store_conn):
    with store_conn.cursor() as cur:
        cur.execute("INSERT INTO pace_events (at, kind, outcome, status) VALUES "
                    "(now(), 'search', 'ok', 200), (now(), 'search', 'refused', 429)")
        cur.execute("DROP TABLE source_requests")
        cur.execute("DELETE FROM kv_state WHERE key = 'schema_version'")
    store_conn.commit()

    init_store(store_conn)

    assert "source_requests" in _tables(store_conn)
    with store_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_indexes WHERE indexname = 'source_requests_source_at_idx'")
        assert cur.fetchone() is not None
        cur.execute("SELECT count(*) FROM pace_events")
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT value FROM kv_state WHERE key = 'schema_version'")
        assert cur.fetchone()[0] == schema_fingerprint()
    # A later start takes the plain-read path and changes nothing.
    init_store(store_conn)
    with store_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pace_events")
        assert cur.fetchone()[0] == 2


def test_jobs_has_the_spec_columns(store_conn):
    expected = {
        "id", "canonical_url", "title", "company", "company_norm", "title_norm", "country",
        "location_raw", "work_mode", "posted_at", "first_seen_at", "last_seen_at",
        "description", "description_status", "description_source", "language", "tier",
        "remote_verdict", "remote_reason", "remote_advice", "score", "summary", "comment",
        "application_channel", "stage", "stage_reason", "attempts", "next_attempt_at",
        "delivered_at", "alerted_at", "archived_at", "posted_text",
    }
    assert expected <= _columns(store_conn, "jobs")


def test_pace_events_has_the_columns_the_schedule_study_needs(store_conn):
    assert {"at", "service", "kind", "outcome", "status", "requests_today"} \
        <= _columns(store_conn, "pace_events")
    assert {"requests_today", "day", "refusal_times", "rest_until", "rest_level"} \
        <= _columns(store_conn, "pace_budgets")


def test_the_always_on_pace_columns_exist(store_conn):
    assert {"refusals_today", "refusal_streak", "request_times", "nudge", "nudged_at",
            "weekday_spacing_seconds", "weekend_spacing_seconds", "calm_days", "last_clean_at"} \
        <= _columns(store_conn, "pace_budgets")
    assert {"spacing_seconds", "wait_seconds"} <= _columns(store_conn, "pace_events")


def test_the_adaptive_pace_columns_exist(store_conn):
    assert "adaptive_state" in _columns(store_conn, "pace_budgets")
    assert "level" in _columns(store_conn, "pace_events")


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


@pytest.mark.parametrize("verdict", ["confirmed", "advised", "not_stated", "rejected",
                                    "not_checked", "unconfirmed"])
def test_every_shipped_verdict_is_accepted_by_the_store(store_conn, verdict):
    # `unconfirmed` is the legacy value old rows carry; `advised`/`not_stated`
    # arrived 2026-10-18 and the constraint was rewritten for them.
    _insert_job(store_conn, remote_verdict=verdict)


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


def _wait_until_waiting_for_access_exclusive(admin, oid, thread, timeout=10.0):
    """True once a backend is queued for an AccessExclusiveLock on `oid`, False
    once `thread` is done (nothing queued)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with admin.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM pg_locks WHERE relation = %s "
                "AND mode = 'AccessExclusiveLock' AND NOT granted", (oid,))
            if cur.fetchone()[0]:
                return True
        if not thread.is_alive():
            return False
        time.sleep(0.02)
    return False


def test_init_store_takes_no_schema_lock_when_the_schema_is_current(
        store_conn, store_conn_factory):
    """Regression for the 2026-10-16 deadlock.

    init_store's idempotent DDL (ALTER TABLE jobs ADD COLUMN IF NOT EXISTS) took
    an AccessExclusiveLock on `jobs` on every service start even when the column
    already existed. A running processor whose open transaction held a plain read
    lock on `jobs` across a LinkedIn pace wait then queued its UPDATE behind that
    AccessExclusiveLock, closing the cycle: the processor waited on init_store,
    init_store waited on the processor's read lock. With the schema current,
    init_store must not queue for the schema lock at all.
    """
    job_id = _insert_job(store_conn)
    store_conn.commit()

    reader = store_conn_factory(store_conn)
    starter = store_conn_factory(store_conn)
    admin = store_conn_factory(store_conn)
    admin.autocommit = True
    try:
        # The running service's transaction: a plain read of `jobs` holds an
        # AccessShareLock until the transaction ends (here, across the fetch).
        reader.autocommit = False
        with reader.cursor() as cur:
            cur.execute("SELECT id FROM jobs WHERE id = %s", (job_id,))
            cur.fetchone()

        with admin.cursor() as cur:
            cur.execute("SELECT 'jobs'::regclass::oid")
            jobs_oid = cur.fetchone()[0]

        failure = {}

        def run_init_store():
            try:
                init_store(starter)
            except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
                failure["init"] = exc

        thread = threading.Thread(target=run_init_store)
        thread.start()
        blocked = _wait_until_waiting_for_access_exclusive(admin, jobs_oid, thread)

        # The service now writes: UPDATE takes a RowExclusiveLock, which queues
        # behind init_store's pending AccessExclusiveLock if there is one.
        update_error = None
        try:
            with reader.cursor() as cur:
                cur.execute("UPDATE jobs SET description = 'x' WHERE id = %s", (job_id,))
            reader.commit()
        except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
            update_error = exc
        thread.join(timeout=30)

        assert not thread.is_alive(), "init_store did not finish"
        assert "init" not in failure, f"init_store failed: {failure['init']!r}"
        assert not isinstance(update_error, psycopg2.errors.DeadlockDetected), (
            "init_store took a schema lock while the schema was current and deadlocked "
            "with a running writer")
        assert update_error is None, f"the service UPDATE failed: {update_error!r}"
        assert not blocked, "init_store waited for an AccessExclusiveLock on jobs"
    finally:
        for conn in (reader, starter, admin):
            conn.close()


def test_an_old_verdict_constraint_is_upgraded_in_place(store_conn):
    """Deploy path for 2026-10-18: an existing store only allowed the four old
    verdicts, so the new one is dropped and recreated by the same init_store."""
    with store_conn.cursor() as cur:
        cur.execute("ALTER TABLE jobs DROP CONSTRAINT jobs_remote_verdict_chk")
        cur.execute(
            "ALTER TABLE jobs ADD CONSTRAINT jobs_remote_verdict_chk CHECK (remote_verdict IS NULL "
            "OR remote_verdict IN ('confirmed','rejected','unconfirmed','not_checked'))")
        cur.execute("DELETE FROM kv_state WHERE key = 'schema_version'")
    store_conn.commit()

    init_store(store_conn)

    _insert_job(store_conn, remote_verdict="advised")
    _insert_job(store_conn, remote_verdict="not_stated")
    with store_conn.cursor() as cur:
        cur.execute("SELECT value FROM kv_state WHERE key = 'schema_version'")
        assert cur.fetchone()[0] == schema_fingerprint()
