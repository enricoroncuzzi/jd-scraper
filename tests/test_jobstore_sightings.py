from datetime import datetime, timedelta, timezone

from src.jobstore.sightings import CardInput, record_sighting

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def _card(**overrides) -> CardInput:
    values = dict(
        source="linkedin", source_job_id="3912345678",
        source_url="https://www.linkedin.com/jobs/view/ai-engineer-at-acme-3912345678",
        title="AI Engineer", company="Acme S.r.l.", location="Milan, Italy",
    )
    values.update(overrides)
    return CardInput(**values)


def _job(conn, job_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT title, company, company_norm, title_norm, country, stage, description, "
            "description_status, canonical_url, first_seen_at, last_seen_at FROM jobs WHERE id = %s",
            (job_id,))
        keys = ("title", "company", "company_norm", "title_norm", "country", "stage",
                "description", "description_status", "canonical_url", "first_seen_at", "last_seen_at")
        return dict(zip(keys, cur.fetchone()))


def test_a_new_card_creates_a_seen_job_and_a_sighting(store_conn):
    result = record_sighting(store_conn, _card(), NOW)
    assert result.created and not result.known
    job = _job(store_conn, result.job_id)
    assert job["stage"] == "seen"
    assert job["company_norm"] == "acme"
    assert job["title_norm"] == "ai engineer"
    assert job["country"] == "italy"
    assert job["description_status"] == "pending" and job["description"] == ""
    assert job["canonical_url"].endswith("3912345678")
    with store_conn.cursor() as cur:
        cur.execute("SELECT source, source_job_id, seen_count FROM sightings WHERE job_id = %s",
                    (result.job_id,))
        assert cur.fetchall() == [("linkedin", "3912345678", 1)]


def test_a_repeated_card_is_known_and_changes_nothing_else(store_conn):
    first = record_sighting(store_conn, _card(), NOW)
    later = NOW + timedelta(hours=5)
    second = record_sighting(store_conn, _card(title="Changed Title"), later)
    assert second.known and not second.created and second.job_id == first.job_id
    job = _job(store_conn, first.job_id)
    assert job["title"] == "AI Engineer"
    assert job["first_seen_at"] == NOW and job["last_seen_at"] == later
    with store_conn.cursor() as cur:
        cur.execute("SELECT seen_count FROM sightings WHERE job_id = %s", (first.job_id,))
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT count(*) FROM jobs")
        assert cur.fetchone()[0] == 1


def test_an_inline_description_is_stored_as_ok(store_conn):
    card = _card(source="freehire", source_job_id="slug-1", source_url="https://freehire.me/jobs/slug-1",
                 description="Build RAG pipelines in Python.", description_source="freehire")
    job = _job(store_conn, record_sighting(store_conn, card, NOW).job_id)
    assert job["description"] == "Build RAG pipelines in Python."
    assert job["description_status"] == "ok"


def test_a_company_page_becomes_the_canonical_url(store_conn):
    card = _card(source="freehire", source_job_id="slug-2", source_url="https://freehire.me/jobs/slug-2",
                 canonical_url="https://boards.greenhouse.io/acme/jobs/55")
    job = _job(store_conn, record_sighting(store_conn, card, NOW).job_id)
    assert job["canonical_url"] == "https://boards.greenhouse.io/acme/jobs/55"


def test_an_unresolvable_location_leaves_country_null(store_conn):
    job = _job(store_conn, record_sighting(store_conn, _card(location="Remote"), NOW).job_id)
    assert job["country"] is None


def test_an_explicit_country_wins_over_the_location_string(store_conn):
    card = _card(source="freehire", source_job_id="slug-3", source_url="https://freehire.me/jobs/slug-3",
                 location="Bergamo", country="italy")
    assert _job(store_conn, record_sighting(store_conn, card, NOW).job_id)["country"] == "italy"


def test_the_tier_hint_is_stored_on_the_job(store_conn):
    result = record_sighting(store_conn, _card(tier_hint=3), NOW)
    with store_conn.cursor() as cur:
        cur.execute("SELECT tier_hint, tier FROM jobs WHERE id = %s", (result.job_id,))
        assert cur.fetchone() == (3, None)


def test_a_non_company_canonical_url_is_ignored_in_favour_of_the_sources_own_page(store_conn):
    card = _card(source="freehire", source_job_id="slug-9", source_url="https://freehire.me/jobs/slug-9",
                 canonical_url="https://it.whatjobs.com/pub_api__cpl__1?utm_source=freehire.me")
    job = _job(store_conn, record_sighting(store_conn, card, NOW).job_id)
    assert job["canonical_url"] == "https://freehire.me/jobs/slug-9"
