import uuid
from datetime import timedelta

import psycopg2
import pytest

from src.delivery.archive import archive_once, backfill_label_job_ids, copy_jobs, init_archive
from tests.delivery_helpers import NOW, scored_job
from tests.legacy_schema import add_offer, add_run, create_legacy_tables


@pytest.fixture
def neon(store_url_fixture):
    """A second schema standing in for Neon: legacy tables exist, archive tables are added."""
    schema = "neon_" + uuid.uuid4().hex[:10]
    admin = psycopg2.connect(store_url_fixture)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema}")
    conn = psycopg2.connect(store_url_fixture, options=f"-c search_path={schema}")
    create_legacy_tables(conn)
    init_archive(conn)
    yield conn
    conn.close()
    with admin.cursor() as cur:
        cur.execute(f"DROP SCHEMA {schema} CASCADE")
    admin.close()


def _neon_count(neon, table):
    with neon.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        return cur.fetchone()[0]


def _archived_at(conn, job_id):
    with conn.cursor() as cur:
        cur.execute("SELECT archived_at FROM jobs WHERE id = %s", (job_id,))
        return cur.fetchone()[0]


def test_only_finished_jobs_are_copied_with_the_same_ids(store_conn, neon):
    done = scored_job(store_conn, 1, 9, stage="delivered")
    scored = scored_job(store_conn, 2, 7)
    expired = scored_job(store_conn, 3, None, stage="expired")
    scored_job(store_conn, 4, None, stage="described")
    scored_job(store_conn, 5, 1, stage="filtered_out", verdict="rejected")
    assert copy_jobs(store_conn, neon, NOW) == 3
    with neon.cursor() as cur:
        cur.execute("SELECT id FROM jobs ORDER BY first_seen_at, id")
        assert {row[0] for row in cur.fetchall()} == {done, scored, expired}
    assert _archived_at(store_conn, done) == NOW and _neon_count(neon, "sightings") == 3


def test_a_second_copy_finds_nothing_to_do(store_conn, neon):
    scored_job(store_conn, 1, 9)
    assert copy_jobs(store_conn, neon, NOW) == 1
    assert copy_jobs(store_conn, neon, NOW + timedelta(minutes=30)) == 0
    assert _neon_count(neon, "jobs") == 1


def test_a_job_changed_after_archiving_is_copied_again_when_archived_at_is_cleared(store_conn, neon):
    job = scored_job(store_conn, 1, 9)
    copy_jobs(store_conn, neon, NOW)
    with store_conn.cursor() as cur:
        cur.execute("UPDATE jobs SET stage = 'delivered', archived_at = NULL WHERE id = %s", (job,))
    store_conn.commit()
    assert copy_jobs(store_conn, neon, NOW + timedelta(days=1)) == 1
    with neon.cursor() as cur:
        cur.execute("SELECT stage FROM jobs WHERE id = %s", (job,))
        assert cur.fetchone()[0] == "delivered"
    assert _neon_count(neon, "jobs") == 1


def test_applications_and_call_history_follow_their_jobs(store_conn, neon):
    job = scored_job(store_conn, 1, 9, stage="delivered")
    call_id = str(uuid.uuid4())
    with store_conn.cursor() as cur:
        cur.execute("INSERT INTO job_applications (job_id, channel, packaged_at, dry_run) "
                    "VALUES (%s, 'external_ats', %s, true)", (job, NOW))
        cur.execute("INSERT INTO llm_calls (id, service, stage, provider, request_model, latency_ms, "
                    "batch_size, attempt, outcome, prompt_version, started_at) VALUES "
                    "(%s, 'process', 'scoring', 'openrouter', 'm', 5, 5, 1, 'ok', 'v', %s)", (call_id, NOW))
        cur.execute("INSERT INTO llm_call_jobs (call_id, job_id) VALUES (%s, %s)", (call_id, job))
    store_conn.commit()
    copy_jobs(store_conn, neon, NOW)
    assert _neon_count(neon, "job_applications") == 1 and _neon_count(neon, "llm_call_jobs") == 1
    with neon.cursor() as cur:
        cur.execute("SELECT service, stage FROM llm_calls WHERE id = %s", (call_id,))
        assert cur.fetchone() == ("process", "scoring")


def test_batches_are_bounded(store_conn, neon):
    for n in range(5):
        scored_job(store_conn, n, 9)
    assert copy_jobs(store_conn, neon, NOW, batch_size=2) == 2
    assert copy_jobs(store_conn, neon, NOW, batch_size=2) == 2
    assert copy_jobs(store_conn, neon, NOW, batch_size=2) == 1


def test_labels_gain_the_job_id_of_their_posting(store_conn, neon):
    job = scored_job(store_conn, 1, 9)
    with store_conn.cursor() as cur:
        cur.execute("UPDATE sightings SET source_url = %s WHERE job_id = %s",
                    ("https://www.linkedin.com/jobs/view/ai-3911111111", job))
    store_conn.commit()
    copy_jobs(store_conn, neon, NOW)
    with neon.cursor() as cur:
        cur.execute("INSERT INTO offer_labels (posting_key, link, label) VALUES "
                    "('linkedin:3911111111', 'https://it.linkedin.com/jobs/view/other-slug-3911111111', 'golden'), "
                    "('linkedin:3999999999', 'https://www.linkedin.com/jobs/view/x-3999999999', 'not_for_me')")
    neon.commit()
    assert backfill_label_job_ids(neon) == 1
    with neon.cursor() as cur:
        cur.execute("SELECT label, job_id FROM offer_labels ORDER BY label")
        assert cur.fetchall() == [("golden", job), ("not_for_me", None)]


def test_an_unreachable_neon_is_reported_not_raised_and_nothing_is_marked(store_conn):
    job = scored_job(store_conn, 1, 9)

    def down():
        raise psycopg2.OperationalError("neon is asleep")

    result = archive_once(store_conn, down, NOW)
    assert result["jobs"] == 0 and "OperationalError" in result["error"]
    assert _archived_at(store_conn, job) is None


def test_a_failure_mid_copy_rolls_both_sides_back(store_conn, neon):
    job = scored_job(store_conn, 1, 9)
    with neon.cursor() as cur:
        cur.execute("DROP TABLE llm_call_jobs")
        cur.execute("DROP TABLE llm_calls")
    neon.commit()
    call_id = str(uuid.uuid4())
    with store_conn.cursor() as cur:
        cur.execute("INSERT INTO llm_calls (id, stage, provider, request_model, latency_ms, batch_size, attempt, "
                    "outcome, prompt_version, started_at) VALUES (%s, 'scoring', 'openrouter', 'm', 1, 1, 1, "
                    "'ok', 'v', %s)", (call_id, NOW))
        cur.execute("INSERT INTO llm_call_jobs (call_id, job_id) VALUES (%s, %s)", (call_id, job))
    store_conn.commit()
    with pytest.raises(psycopg2.Error):
        copy_jobs(store_conn, neon, NOW)
    assert _archived_at(store_conn, job) is None


def test_legacy_tables_are_never_written(store_conn, neon, store_conn_factory):
    run = add_run(neon, 1, NOW)
    add_offer(neon, run, "https://www.linkedin.com/jobs/view/x-3900000001", when=NOW)
    neon.commit()
    scored_job(store_conn, 1, 9)
    result = archive_once(store_conn, lambda: store_conn_factory(neon), NOW)
    assert result["jobs"] == 1 and result["error"] is None
    assert _neon_count(neon, "offers") == 1 and _neon_count(neon, "runs") == 1
