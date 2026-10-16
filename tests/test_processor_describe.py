from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from src.jobstore.sightings import CardInput, record_sighting
from src.linkedin.description import extract_description, extract_posted_text, fetch_description
from src.linkedin.pace import Gate, PaceGateClosed
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


@pytest.mark.parametrize("status", [429, 999])
def test_a_throttled_description_returns_at_once(status):
    get, sleeps, pace = ScriptedGet(response(status)), [], FakePace()
    result = fetch_description(pace, "3900000001", get=get, sleep=sleeps.append)
    assert result.status == "throttled" and result.text == ""
    assert len(get.calls) == 1 and sleeps == [] and pace.events == [("description", "throttled")]


@pytest.mark.parametrize("status", [401, 403, 503, 504])
def test_a_job_specific_refusal_keeps_the_5_15_45_ladder(status):
    get = ScriptedGet(*[response(status)] * 4)
    sleeps, pace = [], FakePace()
    result = fetch_description(pace, "3900000001", get=get, sleep=sleeps.append)
    assert result.status == "refused" and sleeps == [5, 15, 45]
    assert [outcome for _k, outcome in pace.events] == ["neutral"] * 4


def test_999_is_one_refusal_and_ends_the_fetch():
    pace = FakePace()
    get = ScriptedGet(response(999), response(200, FRAGMENT))
    assert fetch_description(pace, "1", get=get, sleep=lambda _s: None).status == "throttled"
    assert len(get.calls) == 1
    assert [outcome for _kind, outcome in pace.events] == ["throttled"]


def test_503_retries_without_slowing_the_pace():
    pace = FakePace()
    get = ScriptedGet(response(503), response(200, FRAGMENT))
    assert fetch_description(pace, "1", get=get, sleep=lambda _s: None).status == "ok"
    assert [outcome for _kind, outcome in pace.events] == ["neutral", "clean"]


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
    get = ScriptedGet(response(429))
    assert run_descriptions(store_conn, FakePace(), cfg, NOW, get=get, sleep=lambda _s: None) == 1
    assert len(get.calls) == 1


def test_a_rate_limited_backlog_is_attempted_once_per_round(store_conn, cfg):
    # A job already rate_limited is refused again without the table count changing,
    # so the round must stop on the fetch outcome itself, not on a count of rows.
    for n in range(5):
        _tiered_job(store_conn, cfg, f"39000000{n + 1:02d}")
    with store_conn.cursor() as cur:
        cur.execute("UPDATE jobs SET description_status = 'rate_limited'")
    get = ScriptedGet(*[response(429)] * 20)
    assert run_descriptions(store_conn, FakePace(), cfg, NOW, get=get, sleep=lambda _s: None) == 1
    assert len(get.calls) == 1


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


def test_a_description_round_beats_before_every_fetch(store_conn, cfg, pulses):
    for n in range(3):
        _tiered_job(store_conn, cfg, f"390000000{n + 1}")
    get = ScriptedGet(*[response(200, FRAGMENT)] * 3)
    assert run_descriptions(store_conn, FakePace(), cfg, NOW, get=get, sleep=lambda _s: None) == 3
    assert [call["describing"] for call in pulses if "describing" in call] == [0, 1, 2, 3]


def test_the_retries_of_one_description_beat_while_it_waits(pulses):
    fetch_description(FakePace(), "3900000009", get=ScriptedGet(*[response(503)] * 4),
                      sleep=lambda _s: None)
    assert pulses == [{"description": "3900000009", "attempt": n} for n in (1, 2, 3, 4)]


def test_a_slow_pace_cannot_keep_a_tick_in_the_description_stage(store_conn, cfg):
    from dataclasses import replace

    for n in range(5):
        _tiered_job(store_conn, cfg, f"390000000{n + 1}")
    times = iter([0.0, 60.0, 130.0, 999.0])  # start, before the 2nd fetch, before the 3rd
    get = ScriptedGet(*[response(200, FRAGMENT)] * 5)
    done = run_descriptions(store_conn, FakePace(), replace(cfg, description_seconds_per_tick=120), NOW,
                            get=get, sleep=lambda _s: None, clock=lambda: next(times))
    assert done == 2 and len(get.calls) == 2


def test_the_first_description_is_fetched_even_when_the_budget_is_zero(store_conn, cfg):
    from dataclasses import replace

    _tiered_job(store_conn, cfg)
    get = ScriptedGet(response(200, FRAGMENT))
    assert run_descriptions(store_conn, FakePace(), replace(cfg, description_seconds_per_tick=0), NOW,
                            get=get, sleep=lambda _s: None) == 1


def test_a_busy_tick_does_not_queue_for_a_far_away_linkedin_turn(store_conn, cfg):
    _tiered_job(store_conn, cfg)
    get = ScriptedGet(response(200, FRAGMENT))
    far = FakePace(wait=300)
    assert run_descriptions(store_conn, far, cfg, NOW, get=get, sleep=lambda _s: None,
                            skip_if_wait_over=120) == 0 and get.calls == []
    assert run_descriptions(store_conn, far, cfg, NOW, get=get, sleep=lambda _s: None) == 1  # an idle tick waits
    near = FakePace(wait=10)
    _tiered_job(store_conn, cfg, "3900000002")
    get = ScriptedGet(response(200, FRAGMENT))
    assert run_descriptions(store_conn, near, cfg, NOW, get=get, sleep=lambda _s: None,
                            skip_if_wait_over=120) == 1


def test_extract_posted_text_reads_the_fragment_marker():
    assert extract_posted_text('<span class="posted-time-ago__text">3 hours ago</span>') == "3 hours ago"
    assert extract_posted_text("<html></html>") == ""


POSTED_FRAGMENT = (f'<span class="posted-time-ago__text">2 days ago</span>'
                   f'<div class="show-more-less-html__markup">{ENGLISH}</div>')


def test_the_posted_ago_text_is_stored_with_the_description(store_conn, cfg):
    job_id = _tiered_job(store_conn, cfg)
    assert describe_next(store_conn, FakePace(), cfg, NOW, get=ScriptedGet(response(200, POSTED_FRAGMENT)),
                         sleep=lambda _s: None) == "described"
    with store_conn.cursor() as cur:
        cur.execute("SELECT posted_text FROM jobs WHERE id = %s", (job_id,))
        assert cur.fetchone()[0] == "2 days ago"


class _ClosedPace:
    """A pace whose schedule gate is closed: every slot raises, no request happens."""

    def gate(self):
        return Gate(open=False, reason="closed_hours", requests_today=0, cap=1400, window="weekday")

    @contextmanager
    def slot(self, kind, *, overdue=False):
        raise PaceGateClosed(self.gate())
        yield


def test_a_closed_gate_skips_the_description_round_without_a_request(store_conn, cfg):
    _tiered_job(store_conn, cfg)
    get = ScriptedGet(response(200, FRAGMENT))
    assert run_descriptions(store_conn, _ClosedPace(), cfg, NOW, get=get, sleep=lambda _s: None) == 0
    assert get.calls == []


def test_a_gate_that_closes_mid_flight_defers_the_job_untouched(store_conn, cfg):
    job_id = _tiered_job(store_conn, cfg)
    assert describe_next(store_conn, _ClosedPace(), cfg, NOW, get=ScriptedGet(response(200, FRAGMENT)),
                         sleep=lambda _s: None) == "deferred"
    assert _state(store_conn, job_id)[:3] == ("seen", "", "pending")


def test_fetch_description_reports_a_gate_that_closed_between_requests():
    assert fetch_description(_ClosedPace(), "1", get=ScriptedGet(), sleep=lambda _s: None).status == "deferred"
