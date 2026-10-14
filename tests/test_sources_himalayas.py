from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import MagicMock


from src.jobstore.sightings import record_sighting
from src.linkedin.catalog import SliceSpec, sync_slices
from src.linkedin.walk import claim_due_slice
from src.sources.base import JsonClient, SourceConfig
from src.sources.himalayas import (
    PAGE_SIZE, build_slices, html_to_text, italy_eligibility, item_to_card, parse_time, walk_slice,
)

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
ALL_TIMEZONES = [-11, -10, -9.5, -9, -8, -7, -6, -5, -4, -3.5, -3, -2, -1, 0, 1, 2, 3, 3.5, 4, 4.5, 5, 5.5,
                 5.75, 6, 6.5, 7, 8, 8.75, 9, 9.5, 10, 10.5, 11, 12, 12.75, 13, 14]

# Modelled on real responses of GET /jobs/api/search (2026-10-14): epoch times, HTML description.
WORLDWIDE = {
    "title": "Senior Applied Machine Learning Engineer - Remote", "companyName": "ClanX",
    "employmentType": "Full Time", "seniority": ["Senior"], "locationRestrictions": [],
    "timezoneRestrictions": ALL_TIMEZONES,
    "description": "<p>FunnelStory is hiring an Applied ML Engineer.</p><ul><li>Own prediction quality</li></ul>",
    "excerpt": "FunnelStory is hiring.", "pubDate": 1791978330, "expiryDate": 1797162329,
    "applicationLink": "https://himalayas.app/companies/clanx/jobs/senior-applied-ml-engineer-remote",
    "guid": "https://himalayas.app/companies/clanx/jobs/senior-applied-ml-engineer-remote",
}
WITH_ITALY = {**WORLDWIDE, "title": "Senior Ruby on Rails Engineer", "companyName": "Recruiter4you",
              "locationRestrictions": ["Albania", "Austria", "Germany", "Italy", "United Kingdom"],
              "timezoneRestrictions": [0, 1, 2, 3, 4], "guid": "https://himalayas.app/companies/r4y/jobs/rails",
              "applicationLink": "https://himalayas.app/companies/r4y/jobs/rails"}
US_ONLY = {**WORLDWIDE, "title": "US Data Scientist", "locationRestrictions": ["United States"],
           "timezoneRestrictions": [-8, -7, -6, -5], "guid": "https://himalayas.app/companies/us/jobs/ds",
           "applicationLink": "https://himalayas.app/companies/us/jobs/ds"}


def _cfg(**options):
    base = {"roles": ["AI Engineer", "ML Engineer"], "country": "IT", "known_pages_to_stop": 2, "max_pages": 4}
    base.update(options)
    return SourceConfig(True, "https://himalayas.app/jobs/api", "owner/project/1.0", 0.0, 24, base)


def test_epoch_iso_and_garbage_times():
    assert parse_time(1791978330) == datetime(2026, 10, 14, 11, 45, 30, tzinfo=timezone.utc)
    assert parse_time("1791978330") == parse_time(1791978330)
    assert parse_time("2026-10-14T11:45:30Z") == parse_time(1791978330)
    assert parse_time("2026-10-14T11:45:30") == parse_time(1791978330)
    assert parse_time(None) is None and parse_time("soon") is None


def test_html_becomes_readable_text():
    assert html_to_text("<p>One</p><ul><li>Two</li></ul>") == "One\nTwo"
    assert html_to_text("") == ""


def test_worldwide_jobs_are_eligible_and_go_to_tier_three():
    e = italy_eligibility(WORLDWIDE)
    assert (e.eligible, e.country, e.tier_hint, e.remote_hint) == (True, None, 3, "remote_worldwide")


def test_a_list_that_names_italy_is_an_italy_job():
    e = italy_eligibility(WITH_ITALY)
    assert (e.eligible, e.country, e.tier_hint, e.remote_hint) == (True, "italy", 1, "remote")


def test_other_country_lists_and_other_time_zones_are_not_eligible():
    assert italy_eligibility(US_ONLY) == italy_eligibility(US_ONLY) and not italy_eligibility(US_ONLY).eligible
    assert italy_eligibility({**WORLDWIDE, "timezoneRestrictions": [-8, -7]}).reason == "timezone"
    assert italy_eligibility({**WORLDWIDE, "locationRestrictions": ["Germany", "France"]}).reason == "location"
    assert italy_eligibility({**WORLDWIDE, "timezoneRestrictions": []}).eligible  # empty means every zone


def test_a_card_carries_the_text_the_page_link_and_no_country_for_worldwide():
    card = item_to_card(WORLDWIDE, 4, NOW)
    assert card.source_job_id == card.source_url == WORLDWIDE["guid"]
    assert card.description == "FunnelStory is hiring an Applied ML Engineer.\nOwn prediction quality"
    assert (card.location, card.country, card.tier_hint, card.source_detail) == ("Worldwide", None, 3, "Full Time")
    assert card.posted_at == datetime(2026, 10, 14, 11, 45, 30, tzinfo=timezone.utc)


def test_the_italy_card_names_italy_even_though_the_list_ends_with_another_country():
    card = item_to_card(WITH_ITALY, 4, NOW)
    assert card.country == "italy" and card.tier_hint == 1 and "Italy" in card.location


def test_expired_ineligible_and_incomplete_jobs_are_not_taken():
    assert item_to_card({**WORLDWIDE, "expiryDate": 1700000000}, 1, NOW) is None
    assert item_to_card(US_ONLY, 1, NOW) is None
    assert item_to_card({**WORLDWIDE, "title": ""}, 1, NOW) is None
    assert item_to_card({**WORLDWIDE, "guid": None, "applicationLink": None}, 1, NOW) is None


def test_an_empty_description_falls_back_to_the_excerpt():
    card = item_to_card({**WORLDWIDE, "description": ""}, 1, NOW)
    assert card.description == "FunnelStory is hiring."


def test_garbage_items_are_skipped_not_raised():
    for garbage in (None, 123, "nope", {"title": 123}, {**WORLDWIDE, "title": "   "},
                    {**WORLDWIDE, "companyName": 5}, {**WORLDWIDE, "guid": 5},
                    {**WORLDWIDE, "timezoneRestrictions": "1"},
                    {**WORLDWIDE, "locationRestrictions": "Italy"},
                    {**WORLDWIDE, "locationRestrictions": ["Italy", None]},
                    {**WORLDWIDE, "locationRestrictions": [None, "Italy"]}):
        assert item_to_card(garbage, 1, NOW) is None


def test_non_text_fields_do_not_skip_the_job():
    card = item_to_card({**WORLDWIDE, "description": 5, "employmentType": {"a": 1}}, 1, NOW)
    assert card is not None
    assert card.description == "FunnelStory is hiring." and card.source_detail is None  # excerpt fallback
    for change in ({"description": ["a"], "excerpt": 5}, {"description": "", "excerpt": ["x"]},
                   {"description": 5, "excerpt": None}):
        card = item_to_card({**WORLDWIDE, **change}, 1, NOW)
        assert card is not None and card.description is None


def test_the_company_page_is_never_claimed_himalayas_is_the_canonical_link(store_conn):
    result = record_sighting(store_conn, item_to_card(WORLDWIDE, None, NOW), NOW)
    with store_conn.cursor() as cur:
        cur.execute("SELECT canonical_url, remote_hint FROM jobs WHERE id = %s", (result.job_id,))
        assert cur.fetchone() == (WORLDWIDE["guid"], "remote_worldwide")


def test_one_slice_per_role_all_in_the_worldwide_tier():
    specs = build_slices(_cfg())
    assert [(s.role, s.area, s.tier) for s in specs] == [("AI Engineer", "country=IT", 3),
                                                         ("ML Engineer", "country=IT", 3)]


class Pages:
    def __init__(self, *bodies):
        self.bodies, self.calls = list(bodies), []

    def __call__(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params)))
        body = self.bodies.pop(0)
        resp = MagicMock()
        resp.status_code = body if isinstance(body, int) else 200
        resp.headers = {}
        resp.json.return_value = {} if isinstance(body, int) else body
        return resp


def _jobs(first, count, template=WORLDWIDE):
    return [{**template, "guid": f"https://himalayas.app/companies/c/jobs/j{first + n}",
             "applicationLink": f"https://himalayas.app/companies/c/jobs/j{first + n}",
             "title": f"AI Engineer {first + n}"} for n in range(count)]


def _walk(conn, pages, cfg=None, **row_overrides):
    cfg = cfg or _cfg()
    sync_slices(conn, [SliceSpec("himalayas", "AI Engineer", "country=IT", 3, 86400)], NOW, "himalayas")
    conn.commit()
    row = replace(claim_due_slice(conn, NOW, "himalayas"), **row_overrides)
    client = JsonClient(cfg, get=pages, sleep=lambda _s: None, clock=lambda: 0.0)
    return walk_slice(conn, client, cfg, row, NOW)


def test_a_walk_asks_for_the_newest_italy_eligible_page_by_page_until_a_short_page(store_conn):
    pages = Pages({"jobs": _jobs(0, PAGE_SIZE)}, {"jobs": _jobs(20, 5)})
    walk = _walk(store_conn, pages)
    assert (walk.pages, walk.cards_seen, walk.new_jobs, walk.stop_reason) == (2, 25, 25, "end_of_results")
    assert pages.calls[0] == ("https://himalayas.app/jobs/api/search",
                              {"q": "AI Engineer", "country": "IT", "sort": "recent", "page": 1})
    assert pages.calls[1][1]["page"] == 2


def test_jobs_nobody_in_italy_can_take_are_counted_as_seen_but_not_recorded(store_conn):
    mixed = _jobs(0, 15) + _jobs(100, 5, US_ONLY)
    walk = _walk(store_conn, Pages({"jobs": mixed}, {"jobs": []}))
    assert walk.cards_seen == 20 and walk.new_jobs == 15


def test_two_full_known_pages_end_the_walk(store_conn):
    for job in _jobs(0, 40):
        record_sighting(store_conn, item_to_card(job, None, NOW), NOW)
    store_conn.commit()
    pages = Pages({"jobs": _jobs(0, 20)}, {"jobs": _jobs(20, 20)}, {"jobs": _jobs(40, 20)})
    walk = _walk(store_conn, pages)
    assert walk.stop_reason == "known_page" and walk.pages == 2 and walk.new_jobs == 0


def test_the_page_cap_ends_a_walk_that_never_runs_dry(store_conn):
    pages = Pages(*[{"jobs": _jobs(n * 20, 20)} for n in range(4)])
    walk = _walk(store_conn, pages)
    assert walk.stop_reason == "cap_hit" and walk.pages == 4


def test_a_throttled_walk_resumes_at_its_page(store_conn):
    pages = Pages({"jobs": _jobs(0, 20)}, 429, 429, 429, 429)
    walk = _walk(store_conn, pages)
    assert walk.stop_reason == "rate_limited" and walk.next_offset == 20
    resumed = Pages({"jobs": []})
    sync_slices(store_conn, [SliceSpec("himalayas", "ML Engineer", "country=IT", 3, 86400)], NOW, "himalayas")
    store_conn.commit()
    row = replace(claim_due_slice(store_conn, NOW, "himalayas"), resume_start=20)
    client = JsonClient(_cfg(), get=resumed, sleep=lambda _s: None, clock=lambda: 0.0)
    walk_slice(store_conn, client, _cfg(), row, NOW)
    assert resumed.calls[0][1]["page"] == 2


def test_a_walk_with_garbage_items_still_records_the_valid_neighbours(store_conn):
    skipping = [None, 123, {"title": 123}, {**WORLDWIDE, "title": "   "},
                {**WORLDWIDE, "timezoneRestrictions": "1"},
                {**WORLDWIDE, "locationRestrictions": ["Italy", None]}]
    coercing = [{**WORLDWIDE, "guid": "https://himalayas.app/companies/c/jobs/g-desc",
                 "applicationLink": "https://himalayas.app/companies/c/jobs/g-desc", "description": 5},
                {**WORLDWIDE, "guid": "https://himalayas.app/companies/c/jobs/g-excerpt",
                 "applicationLink": "https://himalayas.app/companies/c/jobs/g-excerpt",
                 "description": ["a"], "excerpt": 5},
                {**WORLDWIDE, "guid": "https://himalayas.app/companies/c/jobs/g-emp",
                 "applicationLink": "https://himalayas.app/companies/c/jobs/g-emp", "employmentType": {"a": 1}}]
    valid = _jobs(1, 10)
    jobs = skipping + coercing + valid
    pages = Pages({"jobs": jobs}, {"jobs": []})
    walk = _walk(store_conn, pages)
    assert walk.cards_seen == len(jobs) and walk.new_jobs == len(coercing) + len(valid)
    assert walk.stop_reason == "end_of_results"
    with store_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs")
        assert cur.fetchone()[0] == len(coercing) + len(valid)


def test_an_item_that_still_raises_is_skipped(store_conn, monkeypatch):
    from src.sources import himalayas
    real = himalayas.item_to_card

    def flaky(job, slice_id, now):
        if job.get("title") == "explode":
            raise AttributeError("a shape we did not anticipate")
        return real(job, slice_id, now)

    monkeypatch.setattr(himalayas, "item_to_card", flaky)
    pages = Pages({"jobs": [{**WORLDWIDE, "title": "explode"}] + _jobs(1, 10)}, {"jobs": []})
    walk = _walk(store_conn, pages)
    assert walk.cards_seen == 11 and walk.new_jobs == 10 and walk.stop_reason == "end_of_results"


def test_a_non_dict_body_stops_the_walk_with_an_error(store_conn):
    walk = _walk(store_conn, Pages("not json"))
    assert walk.stop_reason == "error" and walk.pages == 0
