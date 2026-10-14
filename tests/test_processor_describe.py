from datetime import datetime, timedelta, timezone

import pytest

from src.jobstore.sightings import CardInput, record_sighting
from src.linkedin.description import extract_description, fetch_description
from src.linkedin.search import RETRY_WAITS
from src.processor.cheap import check_one
from src.processor.config import load_processor_config
from src.processor.describe import describe_next, expire_stale, refusal_delay, run_descriptions
from tests.linkedin_fakes import FakePace, ScriptedGet, response

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
ENGLISH = ("We are looking for an AI Engineer to build retrieval augmented generation pipelines, "
           "evaluate large language models and ship them to production on cloud infrastructure.")
FRAGMENT = f'<div class="decorated-job-posting"><div class="show-more-less-html__markup">{ENGLISH}</div></div>'


@pytest.fixture(scope="module")
def cfg():
    return load_processor_config()


def _tiered_job(conn, cfg, job_id="3900000001", **overrides):
    values = dict(source="linkedin", source_job_id=job_id,
                  source_url=f"https://www.linkedin.com/jobs/view/ai-{job_id}", title=f"AI {job_id}",
                  company="Acme", location="Milan, Italy", tier_hint=1)
    values.update(overrides)
    new_id = record_sighting(conn, CardInput(**values), NOW).job_id
    check_one(conn, new_id, cfg, NOW)
    conn.commit()
    return new_id


def _state(conn, job_id):
    with conn.cursor() as cur:
        cur.execute("SELECT stage, stage_reason, description_status, attempts, next_attempt_at, "
                    "first_refused_at, description_source FROM jobs WHERE id = %s", (job_id,))
        return cur.fetchone()


def test_extract_description_matches_the_old_scraper_exactly():
    assert extract_description(FRAGMENT) == ENGLISH
    assert extract_description('<div class="show-more-less-html__markup"><p>Hello</p><p>World</p></div>') == "HelloWorld"
    assert extract_description("<html></html>") == ""


def test_fetch_requests_the_guest_fragment_not_the_full_page():
    get = ScriptedGet(response(200, FRAGMENT))
    result = fetch_description(FakePace(), "3900000001", get=get, sleep=lambda _s: None)
    assert result.status == "ok" and result.text == ENGLISH


def test_fetch_url_is_the_jobposting_fragment(monkeypatch):
    seen = []

    def get(url, headers=None, timeout=None):
        seen.append(url)
        return response(200, FRAGMENT)
    fetch_description(FakePace(), "3900000001", get=get, sleep=lambda _s: None)
    assert seen == ["https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/3900000001"]


@pytest.mark.parametrize("status", [429, 999, 503, 504, 403])
def test_a_refusal_is_retried_5_15_45_then_reported_as_refused_never_as_text(status):
    get, sleeps = ScriptedGet(*[response(status, "<html>blocked</html>") for _ in range(4)]), []
    result = fetch_description(FakePace(), "1", get=get, sleep=sleeps.append)
    assert result.status == "refused" and result.text == ""
    assert sleeps == list(RETRY_WAITS)


def test_429_and_999_slow_the_pace_but_503_does_not():
    pace = FakePace()
    fetch_description(pace, "1", get=ScriptedGet(response(999), response(503), response(200, FRAGMENT)),
                      sleep=lambda _s: None)
    assert [outcome for _kind, outcome in pace.events] == ["throttled", "neutral", "clean"]


def test_gone_and_unreadable_pages_are_not_retried():
    assert fetch_description(FakePace(), "1", get=ScriptedGet(response(404)), sleep=lambda _s: None).status == "gone"
    page_without_markup = ScriptedGet(response(200, "<html><body><p>Sign in</p></body></html>"))
    assert fetch_description(FakePace(), "1", get=page_without_markup, sleep=lambda _s: None).status == "failed"


def test_refusal_delay_doubles_from_30_minutes_to_12_hours(cfg):
    delays = [refusal_delay(n, cfg) for n in range(1, 8)]
    assert delays[0] == timedelta(minutes=30) and delays[1] == timedelta(hours=1)
    assert delays[2] == timedelta(hours=2) and delays[-1] == timedelta(hours=12)


def test_a_fetched_description_moves_the_job_to_described(store_conn, cfg):
    job_id = _tiered_job(store_conn, cfg)
    assert describe_next(store_conn, FakePace(), cfg, NOW, get=ScriptedGet(response(200, FRAGMENT)),
                         sleep=lambda _s: None) == "described"
    stage, reason, status, attempts, _next, refused, source = _state(store_conn, job_id)
    assert (stage, reason, status, attempts, refused, source) == ("described", "", "ok", 0, None, "linkedin")


def test_a_foreign_language_description_is_filtered_out(store_conn, cfg):
    job_id = _tiered_job(store_conn, cfg)
    german = ('<div class="show-more-less-html__markup">Wir suchen einen erfahrenen Entwickler fuer '
              'unser Team in Berlin, der gemeinsam mit unseren Kunden neue Softwareloesungen '
              'entwickelt und langfristig betreut.</div>')
    describe_next(store_conn, FakePace(), cfg, NOW, get=ScriptedGet(response(200, german)), sleep=lambda _s: None)
    assert _state(store_conn, job_id)[:2] == ("filtered_out", "language:de")


def test_a_refusal_keeps_the_job_in_line_and_doubles_the_wait(store_conn, cfg):
    job_id = _tiered_job(store_conn, cfg)
    for attempt, expected in ((1, timedelta(minutes=30)), (2, timedelta(hours=1))):
        later = NOW + timedelta(days=attempt)
        get = ScriptedGet(*[response(429) for _ in range(4)])
        describe_next(store_conn, FakePace(), cfg, later, get=get, sleep=lambda _s: None)
        stage, _r, status, attempts, next_at, first_refused, _s = _state(store_conn, job_id)
        assert (stage, status, attempts) == ("seen", "rate_limited", attempt)
        assert next_at == later + expected
        assert first_refused == NOW + timedelta(days=1)


def test_a_job_waiting_out_a_backoff_is_not_fetched_early(store_conn, cfg):
    _tiered_job(store_conn, cfg)
    describe_next(store_conn, FakePace(), cfg, NOW, get=ScriptedGet(*[response(429)] * 4), sleep=lambda _s: None)
    get = ScriptedGet()
    assert describe_next(store_conn, FakePace(), cfg, NOW + timedelta(minutes=10), get=get,
                         sleep=lambda _s: None) is None
    assert get.calls == []


def test_gone_postings_expire_and_unreadable_ones_expire_after_three_tries(store_conn, cfg):
    gone = _tiered_job(store_conn, cfg, "3900000001")
    describe_next(store_conn, FakePace(), cfg, NOW, get=ScriptedGet(response(404)), sleep=lambda _s: None)
    assert _state(store_conn, gone)[:3] == ("expired", "posting_gone", "failed")
    bad = _tiered_job(store_conn, cfg, "3900000002")
    for n in range(3):
        later = NOW + timedelta(days=n + 1)
        describe_next(store_conn, FakePace(), cfg, later, get=ScriptedGet(response(200, "<html></html>")),
                      sleep=lambda _s: None)
    assert _state(store_conn, bad)[:3] == ("expired", "description_unreadable", "failed")


def test_tier_one_is_described_before_tier_three(store_conn, cfg):
    three = _tiered_job(store_conn, cfg, "3900000003", location="Berlin, Germany", tier_hint=3)
    one = _tiered_job(store_conn, cfg, "3900000001")
    describe_next(store_conn, FakePace(), cfg, NOW, get=ScriptedGet(response(200, FRAGMENT)), sleep=lambda _s: None)
    assert _state(store_conn, one)[0] == "described" and _state(store_conn, three)[0] == "seen"


def test_a_refusal_ends_the_round_so_the_rest_are_not_hammered(store_conn, cfg):
    for n in range(3):
        _tiered_job(store_conn, cfg, f"390000000{n + 1}")
    get = ScriptedGet(*[response(429)] * 4)
    assert run_descriptions(store_conn, FakePace(), cfg, NOW, get=get, sleep=lambda _s: None) == 1
    assert len(get.calls) == 4


def test_a_rate_limited_backlog_is_attempted_once_per_round(store_conn, cfg):
    # A job already rate_limited is refused again without the table count changing,
    # so the round must stop on the fetch outcome itself, not on a count of rows.
    for n in range(5):
        _tiered_job(store_conn, cfg, f"39000000{n + 1:02d}")
    with store_conn.cursor() as cur:
        cur.execute("UPDATE jobs SET description_status = 'rate_limited'")
    get = ScriptedGet(*[response(429)] * 20)
    assert run_descriptions(store_conn, FakePace(), cfg, NOW, get=get, sleep=lambda _s: None) == 1
    assert len(get.calls) == 4


def test_a_round_fetches_at_most_descriptions_per_tick(store_conn, cfg):
    from dataclasses import replace
    for n in range(3):
        _tiered_job(store_conn, cfg, f"390000000{n + 1}")
    get = ScriptedGet(*[response(200, FRAGMENT)] * 3)
    assert run_descriptions(store_conn, FakePace(), replace(cfg, descriptions_per_tick=2), NOW, get=get,
                            sleep=lambda _s: None) == 2


def test_stale_jobs_expire(store_conn, cfg):
    refused = _tiered_job(store_conn, cfg, "3900000001")
    old = _tiered_job(store_conn, cfg, "3900000002", posted_at=NOW - timedelta(days=40))
    fresh = _tiered_job(store_conn, cfg, "3900000003")
    with store_conn.cursor() as cur:
        cur.execute("UPDATE jobs SET description_status = 'rate_limited', first_refused_at = %s WHERE id = %s",
                    (NOW - timedelta(days=8), refused))
    later = NOW + timedelta(days=1)
    with store_conn.cursor() as cur:
        cur.execute("UPDATE jobs SET first_seen_at = %s WHERE id = ANY(%s::uuid[])", (later, [refused, old, fresh]))
    assert expire_stale(store_conn, cfg, later) == 2
    assert _state(store_conn, refused)[:2] == ("expired", "description_refused")
    assert _state(store_conn, old)[:2] == ("expired", "posting_too_old")
    assert _state(store_conn, fresh)[0] == "seen"
