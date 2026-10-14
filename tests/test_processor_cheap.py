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


def test_the_title_prefilter_is_off_by_default_and_filters_when_switched_on(store_conn, cfg):
    from dataclasses import replace
    job_id = record_sighting(store_conn, _card(title="Principal ML Engineer"), NOW).job_id
    assert check_one(store_conn, job_id, cfg, NOW) == "tiered"
    on = replace(cfg, policies={**cfg.policies, 1: replace(cfg.policies[1], title_prefilter=True)})
    second = record_sighting(store_conn, _card("222222", title="Principal ML Engineer"), NOW).job_id
    assert check_one(store_conn, second, on, NOW) == "filtered"
    assert _job(store_conn, second)[:2] == ("filtered_out", "title_prefilter:Principal")


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
