from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from src.linkedin.catalog import sync_slices
from src.linkedin.walk import claim_due_slice
from src.sources.base import JsonClient, SourceConfig, SourceError
from src.sources.freehire import (
    SLICE_AREAS, build_slices, clean_text, freehire_country, item_to_card, posted_window_days, walk_slice,
)
from src.jobstore.sightings import record_sighting

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)

# Modelled on real responses of GET /api/v1/jobs/search (2026-10-14).
AGGREGATOR_HIT = {
    "public_slug": "ai-software-engineer-remote-python-llms-datapizza-s-r-l-eif6yvcx",
    "source": "whatjobs-it", "external_id": "python developer:213084416",
    "url": "https://it.whatjobs.com/pub_api__cpl__213084416__7128?geoID=5187&utm_source=freehire.me",
    "title": "AI Software Engineer - Remote, Python & LLMs", "company": "Datapizza S.r.l.",
    "location": "Bergamo, Italy",
    "description": "Una startup cerca un AI Software Engineer. L&#39;offerta prevede un contratto full remote.",
    "countries": ["it"], "regions": ["eu"], "posted_at": "2026-10-13T08:21:22Z", "closed_at": None,
    "enrichment": {"category": "ai_engineering", "posting_language": "it"}, "reality": {"class": "fresh"},
}
ATS_HIT = {
    "public_slug": "ml-engineer-acme-ab12", "source": "greenhouse",
    "url": "https://job-boards.greenhouse.io/acme/jobs/4455", "title": "ML Engineer", "company": "Acme GmbH",
    "location": "Berlin, Germany or remote EU", "description": "<p>Build RAG pipelines.</p><p>Fully remote.</p>",
    "countries": ["de", "fr"], "work_mode": "remote", "posted_at": "2026-10-12T10:00:00Z",
    "reality": {"class": "fresh"},
}


def _cfg(**options):
    base = {"categories": ["ai_engineering", "data_science"], "page_size": 2, "first_pull_days": 30,
            "known_pages_to_stop": 2, "exclude_sources": ["whatjobs-it"], "skip_reality": ["likely-evergreen"]}
    base.update(options)
    return SourceConfig(True, "https://freehire.me/api/v1", "owner/project/1.0", 0.0, 24, base)


class Pages:
    def __init__(self, *bodies):
        self.bodies, self.calls = list(bodies), []

    def __call__(self, url, params=None, headers=None, timeout=None):
        self.calls.append(list(params))
        body = self.bodies.pop(0)
        resp = MagicMock()
        resp.status_code = body if isinstance(body, int) else 200
        resp.headers = {}
        resp.json.return_value = {} if isinstance(body, int) else body
        return resp


def _hits(first, count):
    return [{**ATS_HIT, "public_slug": f"slug-{first + n}", "title": f"ML Engineer {first + n}"}
            for n in range(count)]


def _body(items):
    return {"data": items, "meta": {"total": 999, "limit": 2, "offset": 0}}


def _walk(conn, pages, cfg=None, tier=1, area="countries=it", **row_overrides):
    cfg = cfg or _cfg()
    from src.linkedin.catalog import SliceSpec
    sync_slices(conn, [SliceSpec("freehire", "ai_engineering", area, tier, 86400)], NOW, "freehire")
    conn.commit()
    row = replace(claim_due_slice(conn, NOW, "freehire"), **row_overrides)
    client = JsonClient(cfg, get=pages, sleep=lambda _s: None, clock=lambda: 0.0)
    return walk_slice(conn, client, cfg, row, NOW)


def test_country_mapping():
    assert freehire_country(["it"]) == "italy"
    assert freehire_country(["de", "it", "fr"]) == "italy"
    assert freehire_country(["de"]) == "germany"
    assert freehire_country(["de", "fr"]) is None
    assert freehire_country(["xx"]) is None
    assert freehire_country([]) is None


def test_clean_text_decodes_entities_and_drops_tags():
    assert clean_text("L&#39;offerta &amp; il resto") == "L'offerta & il resto"
    assert clean_text("<p>Hello</p>\n\n\n\n<p>World</p>").split() == ["Hello", "World"]


def test_a_hit_becomes_a_card_with_the_description_the_hint_and_the_origin():
    card = item_to_card(ATS_HIT, slice_id=7, tier_hint=3)
    assert (card.source, card.source_job_id) == ("freehire", "ml-engineer-acme-ab12")
    assert card.source_url == "https://freehire.me/jobs/ml-engineer-acme-ab12"
    assert card.canonical_url == "https://job-boards.greenhouse.io/acme/jobs/4455"
    assert (card.remote_hint, card.country, card.tier_hint, card.source_detail) == ("remote", None, 3, "greenhouse")
    assert card.description.split() == ["Build", "RAG", "pipelines.", "Fully", "remote."]
    assert card.posted_at == datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)


def test_a_hit_that_lists_italy_is_an_italy_job():
    assert item_to_card({**ATS_HIT, "countries": ["de", "it"]}, 1, 3).country == "italy"


def test_an_aggregator_tracking_url_never_becomes_the_canonical_link(store_conn):
    card = item_to_card(AGGREGATOR_HIT, None, 1)
    result = record_sighting(store_conn, card, NOW)
    with store_conn.cursor() as cur:
        cur.execute("SELECT canonical_url, description FROM jobs WHERE id = %s", (result.job_id,))
        canonical, description = cur.fetchone()
    assert canonical == "https://freehire.me/jobs/ai-software-engineer-remote-python-llms-datapizza-s-r-l-eif6yvcx"
    assert "L'offerta" in description


@pytest.mark.parametrize("change", [
    {"closed_at": "2026-10-01T00:00:00Z"}, {"reality": {"class": "likely-evergreen"}},
    {"public_slug": ""}, {"title": None}, {"company": ""},
])
def test_hits_we_do_not_take(change):
    assert item_to_card({**ATS_HIT, **change}, 1, 1, skip_reality=("likely-evergreen",)) is None


def test_the_slice_catalog_covers_four_tiers_and_two_categories():
    specs = build_slices(_cfg())
    assert len(specs) == 8 and {s.tier for s in specs} == {1, 2, 3, 4}
    assert {s.area for s in specs} == set(SLICE_AREAS.values())
    assert all(s.base_interval_seconds == 86400 and s.source == "freehire" for s in specs)


def test_the_posted_window_is_30_days_first_then_days_since_plus_two():
    from src.linkedin.walk import SliceRow
    row = SliceRow(1, "x", "y", 1, 86400, 86400, None, 0, 0)
    assert posted_window_days(_cfg(), row, NOW) == 30
    assert posted_window_days(_cfg(), replace(row, last_walked_at=NOW - timedelta(hours=26)), NOW) == 3
    assert posted_window_days(_cfg(), replace(row, last_walked_at=NOW - timedelta(hours=1)), NOW) == 2


def test_a_walk_pages_until_a_short_page_and_asks_for_filtered_newest_first_full_text(store_conn):
    pages = Pages(_body(_hits(0, 2)), _body(_hits(2, 2)), _body(_hits(4, 1)))
    walk = _walk(store_conn, pages)
    assert (walk.pages, walk.cards_seen, walk.new_jobs, walk.stop_reason) == (3, 5, 5, "end_of_results")
    first = pages.calls[0]
    assert ("category", "ai_engineering") in first and ("countries", "it") in first
    assert ("source_exclude", "whatjobs-it") in first and ("description_format", "text") in first
    assert ("sort", "posted_at") in first and ("order", "desc") in first and ("limit", 2) in first
    assert ("posted_within_days", 30) in first
    assert [dict(call)["offset"] for call in pages.calls] == [0, 2, 4]
    with store_conn.cursor() as cur:
        cur.execute("SELECT count(*), count(*) FILTER (WHERE description_status = 'ok'), "
                    "min(tier_hint) FROM jobs")
        assert cur.fetchone() == (5, 5, 1)
        cur.execute("SELECT DISTINCT source_detail FROM sightings")
        assert cur.fetchall() == [("greenhouse",)]


def test_two_full_pages_of_known_jobs_end_the_walk(store_conn):
    for item in _hits(0, 4):
        record_sighting(store_conn, item_to_card(item, None, 1), NOW)
    store_conn.commit()
    pages = Pages(_body(_hits(0, 2)), _body(_hits(2, 2)), _body(_hits(4, 2)))
    walk = _walk(store_conn, pages)
    assert walk.stop_reason == "known_page" and walk.pages == 2 and walk.new_jobs == 0
    assert len(pages.bodies) == 1


def test_a_filter_the_api_ignored_stops_the_walk_loudly(store_conn):
    body = {**_body(_hits(0, 2)), "meta": {"total": 1475149, "ignored_params": [{"param": "country"}]}}
    with pytest.raises(SourceError, match="ignored parameters"):
        _walk(store_conn, Pages(body))
    with store_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs")
        assert cur.fetchone()[0] == 0


def test_a_throttled_walk_is_unfinished_and_keeps_its_offset(store_conn):
    pages = Pages(_body(_hits(0, 2)), *[429, 429, 429, 429])
    walk = _walk(store_conn, pages)
    assert walk.stop_reason == "rate_limited" and not walk.finished and walk.next_offset == 2


def test_the_ten_thousand_result_ceiling_ends_the_walk_without_a_request(store_conn):
    pages = Pages()
    walk = _walk(store_conn, pages, _cfg(page_size=3000), resume_start=9000)
    assert walk.stop_reason == "cap_hit" and pages.calls == []
