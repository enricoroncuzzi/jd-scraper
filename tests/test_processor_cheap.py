from datetime import datetime, timedelta, timezone

import pytest

from src.jobstore.sightings import CardInput, record_sighting
from src.processor.cheap import (
    TIER_BY_COUNTRY, check_one, legacy_hash, provisional_tier, run_cheap_checks, title_hits_exclude,
)
from src.processor.config import load_processor_config
from src.processor.language import classify_language

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
ENGLISH = ("We are looking for an AI Engineer to build retrieval augmented generation pipelines, "
           "evaluate large language models and ship them to production on cloud infrastructure.")
GERMAN = ("Wir suchen einen erfahrenen Entwickler fuer unser Team in Berlin, der gemeinsam mit "
          "unseren Kunden neue Softwareloesungen entwickelt und langfristig betreut.")


@pytest.fixture(scope="module")
def cfg():
    return load_processor_config()


def _card(job_id="111111", **overrides):
    values = dict(source="linkedin", source_job_id=job_id,
                  source_url=f"https://www.linkedin.com/jobs/view/ai-engineer-{job_id}",
                  title="AI Engineer", company="Acme", location="Milan, Italy", tier_hint=1)
    values.update(overrides)
    return CardInput(**values)


def _job(conn, job_id):
    with conn.cursor() as cur:
        cur.execute("SELECT stage, stage_reason, tier, language FROM jobs WHERE id = %s", (job_id,))
        return cur.fetchone()


def _job_class(conn, job_id):
    with conn.cursor() as cur:
        cur.execute("SELECT description_class FROM jobs WHERE id = %s", (job_id,))
        return cur.fetchone()[0]


def test_the_country_to_tier_map_covers_every_tier():
    assert TIER_BY_COUNTRY["italy"] == 1 and TIER_BY_COUNTRY["switzerland"] == 2
    assert TIER_BY_COUNTRY["san marino"] == 2 and TIER_BY_COUNTRY["united kingdom"] == 4
    assert TIER_BY_COUNTRY["germany"] == 3 and TIER_BY_COUNTRY["norway"] == 3
    assert "united states" not in TIER_BY_COUNTRY


@pytest.mark.parametrize("country,hint,expected", [
    ("italy", 3, 1), ("germany", 1, 3), (None, 3, 3), (None, None, None), ("united states", 4, None),
])
def test_provisional_tier(country, hint, expected):
    assert provisional_tier(country, hint) == expected


@pytest.mark.parametrize("title,expected", [
    ("Principal ML Engineer", "Principal"), ("Staff Data Scientist", "Staff"),
    ("AI Engineer (C++)", "C++"), ("Java Developer", "Java"), ("VP of AI", "VP"),
    ("JavaScript Engineer", None), ("Staffing Coordinator", None), ("AI Engineer", None),
])
def test_title_hits_exclude_matches_whole_words_only(cfg, title, expected):
    assert title_hits_exclude(title, cfg.policies[1].exclude_keywords) == expected


def test_language_parity_with_the_old_filter():
    assert classify_language(ENGLISH) == ("en", True)
    assert classify_language(GERMAN) == ("de", False)
    assert classify_language("too short") == (None, True)


def test_a_job_in_scope_gets_its_tier_and_waits_for_a_description(store_conn, cfg):
    job_id = record_sighting(store_conn, _card(), NOW).job_id
    assert check_one(store_conn, job_id, cfg, NOW) == "tiered"
    assert _job(store_conn, job_id) == ("seen", "", 1, None)


def test_a_known_country_outside_every_tier_is_filtered_out(store_conn, cfg):
    job_id = record_sighting(store_conn, _card(location="Austin, Texas, United States"), NOW).job_id
    assert check_one(store_conn, job_id, cfg, NOW) == "filtered"
    assert _job(store_conn, job_id)[:2] == ("filtered_out", "out_of_scope")


def test_an_unresolvable_location_falls_back_to_the_slice_tier(store_conn, cfg):
    job_id = record_sighting(store_conn, _card(location="Europe", tier_hint=3), NOW).job_id
    check_one(store_conn, job_id, cfg, NOW)
    assert _job(store_conn, job_id)[2] == 3


def test_the_title_prefilter_is_on_for_the_shipped_config_and_filters_strict_titles(store_conn, cfg):
    job_id = record_sighting(store_conn, _card(title="Principal ML Engineer"), NOW).job_id
    assert check_one(store_conn, job_id, cfg, NOW) == "filtered"
    assert _job(store_conn, job_id)[:2] == ("filtered_out", "title_prefilter:Principal")


def test_the_prefilter_does_not_drop_senior_lead_or_head_of_titles(store_conn, cfg):
    for source_job_id, title in (("444444", "Senior AI Engineer"), ("444445", "Lead AI Engineer"),
                                 ("444446", "Head of AI")):
        job_id = record_sighting(store_conn, _card(source_job_id, title=title), NOW).job_id
        assert check_one(store_conn, job_id, cfg, NOW) == "tiered", title


def test_a_link_the_old_system_handled_is_skipped(store_conn, cfg):
    card = _card()
    with store_conn.cursor() as cur:
        cur.execute("INSERT INTO legacy_seen_hashes (link_hash, tier) VALUES (%s, 1)",
                    (legacy_hash(card.source_url),))
    job_id = record_sighting(store_conn, card, NOW).job_id
    assert check_one(store_conn, job_id, cfg, NOW) == "filtered"
    assert _job(store_conn, job_id)[:2] == ("filtered_out", "seen_before_cutover")


def test_an_inline_description_goes_straight_to_the_language_check(store_conn, cfg):
    good = record_sighting(store_conn, _card(source="freehire", source_job_id="a", source_url="https://freehire.me/a",
                                             description=ENGLISH, description_source="freehire"), NOW).job_id
    bad = record_sighting(store_conn, _card("333333", source="freehire", source_job_id="b",
                                            source_url="https://freehire.me/b", description=GERMAN,
                                            description_source="freehire", title="Entwickler",
                                            company="Other"), NOW).job_id
    check_one(store_conn, good, cfg, NOW)
    check_one(store_conn, bad, cfg, NOW)
    assert _job(store_conn, good) == ("described", "", 1, "en")
    assert _job(store_conn, bad)[:2] == ("filtered_out", "language:de")


def test_a_clear_cross_source_twin_is_merged_before_any_description_is_paid_for(store_conn, cfg):
    linkedin = record_sighting(store_conn, _card(), NOW).job_id
    freehire = record_sighting(store_conn, _card(
        source="freehire", source_job_id="fh", source_url="https://freehire.me/fh",
        canonical_url="https://boards.greenhouse.io/acme/jobs/1", country="italy",
        description=ENGLISH, description_source="freehire"), NOW + timedelta(hours=1)).job_id
    assert check_one(store_conn, freehire, cfg, NOW) == "gone"
    assert check_one(store_conn, linkedin, cfg, NOW) == "tiered"
    stage, _reason, tier, language = _job(store_conn, linkedin)
    assert (stage, tier, language) == ("described", 1, "en")  # LinkedIn's description fetch is saved
    with store_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs")
        assert cur.fetchone()[0] == 1


def test_run_cheap_checks_handles_oldest_first_and_skips_jobs_merged_away(store_conn, cfg):
    from dataclasses import replace
    record_sighting(store_conn, _card("1"), NOW)
    record_sighting(store_conn, _card("2", title="ML Engineer"), NOW)
    store_conn.commit()
    assert run_cheap_checks(store_conn, replace(cfg, cheap_checks_per_tick=1), NOW) == 1
    assert run_cheap_checks(store_conn, cfg, NOW) == 1
    assert run_cheap_checks(store_conn, cfg, NOW) == 0


def test_an_expired_described_twin_keeps_its_model_rows_and_verdict(store_conn, cfg):
    from src.processor.describe import expire_stale
    old = record_sighting(store_conn, _card(posted_at=NOW - timedelta(days=40)), NOW).job_id
    check_one(store_conn, old, cfg, NOW)
    with store_conn.cursor() as cur:
        cur.execute("UPDATE jobs SET remote_verdict = 'confirmed', remote_reason = 'stable' WHERE id = %s",
                    (old,))
        cur.execute("INSERT INTO llm_calls (id, stage, provider, request_model, latency_ms, batch_size, "
                    "attempt, outcome, prompt_version, started_at) VALUES "
                    "(gen_random_uuid(), 'verification', 'openrouter', 'm', 1, 1, 1, 'ok', 'v', %s) "
                    "RETURNING id", (NOW,))
        call_id = cur.fetchone()[0]
        cur.execute("INSERT INTO llm_call_jobs (call_id, job_id) VALUES (%s, %s)", (call_id, old))
    store_conn.commit()
    assert expire_stale(store_conn, cfg, NOW) == 1
    store_conn.commit()
    fresh = record_sighting(store_conn, _card(
        "777777", source="freehire", source_job_id="fh", source_url="https://freehire.me/fh",
        canonical_url="https://boards.greenhouse.io/acme/jobs/1", country="italy",
        description=ENGLISH, description_source="freehire"), NOW + timedelta(hours=1)).job_id
    assert check_one(store_conn, fresh, cfg, NOW + timedelta(hours=1)) == "tiered"
    store_conn.commit()
    with store_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM llm_call_jobs")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT remote_verdict FROM jobs WHERE id = %s", (fresh,))
        assert cur.fetchone()[0] == "confirmed"


@pytest.mark.parametrize("title, location, tier_hint, expected", [
    ("AI Engineer", "Italy", 1, 0),
    ("AI Engineer", "Milan, Italy", 1, 1),
    ("Python Developer", "Italy", 1, 2),
    ("Python Developer", "Milan, Italy", 1, 3),
    ("Senior AI Engineer", "Italy", 1, 2),
    ("AI Engineer", "Europe", 1, 1),
    ("AI Engineer", "Zurich, Switzerland", 2, 0),
    ("AI Engineer", "Switzerland", 2, 0),
    ("Python Developer", "Zurich, Switzerland", 2, 1),
])
def test_a_tiered_job_is_given_its_tier_s_priority_class(store_conn, cfg, title, location,
                                                        tier_hint, expected):
    job_id = record_sighting(store_conn, _card("222222", title=title, location=location,
                                               tier_hint=tier_hint), NOW).job_id
    assert check_one(store_conn, job_id, cfg, NOW) == "tiered"
    assert _job_class(store_conn, job_id) == expected


def test_an_out_of_scope_job_is_never_classified(store_conn, cfg):
    job_id = record_sighting(store_conn, _card("333333", location="Austin, Texas, United States"),
                             NOW).job_id
    assert check_one(store_conn, job_id, cfg, NOW) == "filtered"
    assert _job_class(store_conn, job_id) is None
