from datetime import datetime, timedelta, timezone

import pytest

from src.jobstore.merge import find_twin, merge_jobs, resolve_identity
from src.jobstore.sightings import CardInput, record_sighting

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def _linkedin(job_no="111111", **overrides):
    values = dict(source="linkedin", source_job_id=job_no,
                  source_url=f"https://www.linkedin.com/jobs/view/ai-engineer-{job_no}",
                  title="AI Engineer", company="Acme S.r.l.", location="Milan, Italy")
    values.update(overrides)
    return CardInput(**values)


def _freehire(slug="fh-1", **overrides):
    values = dict(source="freehire", source_job_id=slug, source_url=f"https://freehire.me/jobs/{slug}",
                  canonical_url="https://boards.greenhouse.io/acme/jobs/9",
                  title="AI Engineer", company="ACME SRL", location="Milano", country="italy",
                  description="Full text from freehire.", description_source="freehire")
    values.update(overrides)
    return CardInput(**values)


def _stage(conn, job_id, stage):
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET stage = %s WHERE id = %s", (stage, job_id))


def _count(conn, table="jobs"):
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        return cur.fetchone()[0]


def test_clear_cross_source_twin_is_found(store_conn):
    a = record_sighting(store_conn, _linkedin(), NOW).job_id
    b = record_sighting(store_conn, _freehire(), NOW + timedelta(hours=3)).job_id
    assert find_twin(store_conn, b) == a
    assert find_twin(store_conn, a) == b


def test_same_source_postings_are_never_twins(store_conn):
    a = record_sighting(store_conn, _linkedin("111111"), NOW).job_id
    b = record_sighting(store_conn, _linkedin("222222"), NOW).job_id
    assert find_twin(store_conn, a) is None
    assert find_twin(store_conn, b) is None


def test_unknown_country_is_not_clearly_met(store_conn):
    a = record_sighting(store_conn, _linkedin(location="Remote"), NOW).job_id
    b = record_sighting(store_conn, _freehire(country=None, location="Remote"), NOW).job_id
    assert find_twin(store_conn, a) is None and find_twin(store_conn, b) is None


def test_different_country_title_or_company_stay_separate(store_conn):
    a = record_sighting(store_conn, _linkedin(), NOW).job_id
    for slug, change in (("c", {"country": "germany", "location": "Berlin"}),
                         ("t", {"title": "ML Engineer"}),
                         ("o", {"company": "Other Corp"})):
        b = record_sighting(store_conn, _freehire(slug, **change), NOW).job_id
        assert find_twin(store_conn, b) is None, slug
    assert find_twin(store_conn, a) is None


def test_more_than_30_days_apart_stays_separate(store_conn):
    a = record_sighting(store_conn, _linkedin(), NOW).job_id
    b = record_sighting(store_conn, _freehire(), NOW + timedelta(days=31)).job_id
    assert find_twin(store_conn, b) is None and find_twin(store_conn, a) is None


def test_two_candidates_is_ambiguous_so_nothing_merges(store_conn):
    record_sighting(store_conn, _linkedin("111111"), NOW)
    record_sighting(store_conn, _linkedin("222222"), NOW)
    c = record_sighting(store_conn, _freehire(), NOW).job_id
    assert find_twin(store_conn, c) is None


def test_merge_moves_sightings_prefers_the_company_page_and_keeps_the_description(store_conn):
    a = record_sighting(store_conn, _linkedin(), NOW).job_id
    b = record_sighting(store_conn, _freehire(), NOW + timedelta(hours=3)).job_id
    survivor = resolve_identity(store_conn, b)
    assert survivor == a
    assert _count(store_conn) == 1
    with store_conn.cursor() as cur:
        cur.execute("SELECT canonical_url, description, description_status, description_source, "
                    "first_seen_at FROM jobs WHERE id = %s", (survivor,))
        canonical, description, status, source, first_seen = cur.fetchone()
        cur.execute("SELECT source FROM sightings WHERE job_id = %s ORDER BY source", (survivor,))
        sources = [row[0] for row in cur.fetchall()]
    assert canonical == "https://boards.greenhouse.io/acme/jobs/9"
    assert (description, status, source) == ("Full text from freehire.", "ok", "freehire")
    assert first_seen == NOW
    assert sources == ["freehire", "linkedin"]


@pytest.mark.parametrize("old_stage", ["scored", "delivered", "filtered_out"])
def test_a_decided_job_survives_and_keeps_its_verdict(store_conn, old_stage):
    a = record_sighting(store_conn, _linkedin(), NOW).job_id
    _stage(store_conn, a, old_stage)
    b = record_sighting(store_conn, _freehire(), NOW + timedelta(hours=1)).job_id
    assert resolve_identity(store_conn, b) == a
    with store_conn.cursor() as cur:
        cur.execute("SELECT stage FROM jobs WHERE id = %s", (a,))
        assert cur.fetchone()[0] == old_stage


def test_an_expired_job_is_revived_by_a_sighting_with_a_description(store_conn):
    a = record_sighting(store_conn, _linkedin(), NOW).job_id
    _stage(store_conn, a, "expired")
    b = record_sighting(store_conn, _freehire(), NOW + timedelta(hours=1)).job_id
    survivor = resolve_identity(store_conn, b)
    with store_conn.cursor() as cur:
        cur.execute("SELECT stage, description_status FROM jobs WHERE id = %s", (survivor,))
        assert cur.fetchone() == ("seen", "ok")


def test_merge_jobs_picks_the_same_survivor_in_either_argument_order(store_conn):
    a = record_sighting(store_conn, _linkedin(), NOW).job_id
    b = record_sighting(store_conn, _freehire(), NOW + timedelta(hours=1)).job_id
    assert merge_jobs(store_conn, b, a) == a
    assert _count(store_conn) == 1


def test_equal_first_seen_prefers_the_job_with_a_description_in_either_order(store_conn):
    a = record_sighting(store_conn, _linkedin(), NOW).job_id
    b = record_sighting(store_conn, _freehire(), NOW).job_id
    assert merge_jobs(store_conn, a, b) == b
    c = record_sighting(store_conn, _linkedin("333333", company="Beta"), NOW).job_id
    d = record_sighting(store_conn, _freehire("fh-2", company="Beta"), NOW).job_id
    assert merge_jobs(store_conn, d, c) == d


def test_merge_repoints_child_rows_and_carries_the_paid_verdict(store_conn):
    old = record_sighting(store_conn, _linkedin(), NOW).job_id
    with store_conn.cursor() as cur:
        cur.execute("UPDATE jobs SET stage = 'expired', stage_reason = 'posting_too_old', "
                    "remote_verdict = 'confirmed', remote_reason = 'stable', score = 9, summary = 's', "
                    "comment = 'c', language = 'en' WHERE id = %s", (old,))
        cur.execute("INSERT INTO llm_calls (id, stage, provider, request_model, latency_ms, batch_size, "
                    "attempt, outcome, prompt_version, started_at) VALUES "
                    "(gen_random_uuid(), 'verification', 'openrouter', 'm', 1, 1, 1, 'ok', 'v', %s) "
                    "RETURNING id", (NOW,))
        call_id = cur.fetchone()[0]
        cur.execute("INSERT INTO llm_call_jobs (call_id, job_id) VALUES (%s, %s)", (call_id, old))
        cur.execute("INSERT INTO job_applications (job_id, channel) VALUES (%s, 'external_ats')", (old,))
    fresh = record_sighting(store_conn, _freehire(), NOW + timedelta(hours=1)).job_id
    assert resolve_identity(store_conn, fresh) == fresh  # seen outranks expired, so the fresh row survives
    with store_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs WHERE id = %s", (old,))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT job_id FROM llm_call_jobs")
        assert [row[0] for row in cur.fetchall()] == [fresh]
        cur.execute("SELECT job_id, channel FROM job_applications")
        assert cur.fetchall() == [(fresh, "external_ats")]
        cur.execute("SELECT remote_verdict, remote_reason, score, summary, comment, language "
                    "FROM jobs WHERE id = %s", (fresh,))
        assert cur.fetchone() == ("confirmed", "stable", 9, "s", "c", "en")
