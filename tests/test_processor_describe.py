import json
import re
from dataclasses import replace
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
import requests

from src.jobstore.sightings import CardInput, record_sighting
from src.linkedin.description import extract_description, extract_posted_text, fetch_description
from src.linkedin.pace import Gate, PaceGateClosed, linkedin_pace
from src.linkedin.schedule import ScheduleConfig
from src.processor import describe
from src.processor.cheap import check_one
from src.processor.config import load_processor_config
from src.processor.describe import (classify_waiting, describe_next, expire_stale, refusal_delay,
                                    run_descriptions)
from tests.linkedin_fakes import BusyPace, FakePace, ScriptedGet, response

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
        get = ScriptedGet(*[response(403) for _ in range(4)])
        describe_next(store_conn, FakePace(), cfg, later, get=get, sleep=lambda _s: None)
        stage, _r, status, attempts, next_at, first_refused, _s = _state(store_conn, job_id)
        assert (stage, status, attempts) == ("seen", "rate_limited", attempt)
        assert next_at == later + expected
        assert first_refused == NOW + timedelta(days=1)


def test_a_job_waiting_out_a_backoff_is_not_fetched_early(store_conn, cfg):
    _tiered_job(store_conn, cfg)
    describe_next(store_conn, FakePace(), cfg, NOW, get=ScriptedGet(*[response(403)] * 4), sleep=lambda _s: None)
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


def test_a_tie_between_tiers_goes_to_tier_one(store_conn, cfg):
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
        return Gate(open=False, reason="closed_hours", requests_today=0)

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


# -- host throttles carry no job penalty; the 24 h deadline guard (design 4.2/4.3) -------

def test_a_throttled_description_goes_back_to_the_queue_without_a_penalty(store_conn, cfg):
    job_id = _tiered_job(store_conn, cfg)
    before = _state(store_conn, job_id)[4]
    assert describe_next(store_conn, FakePace(), cfg, NOW, get=ScriptedGet(response(429)),
                         sleep=lambda _s: None) == "throttled"
    stage, _r, status, attempts, next_at, refused_at, _src = _state(store_conn, job_id)
    assert (stage, status, attempts, next_at, refused_at) == ("seen", "rate_limited", 0, before, NOW)
    get = ScriptedGet(response(200, FRAGMENT))
    assert describe_next(store_conn, FakePace(), cfg, NOW, get=get, sleep=lambda _s: None) == "described"


def test_a_throttled_job_still_expires_seven_days_after_its_first_refusal(store_conn, cfg):
    job_id = _tiered_job(store_conn, cfg)
    describe_next(store_conn, FakePace(), cfg, NOW, get=ScriptedGet(response(429)), sleep=lambda _s: None)
    with store_conn.cursor() as cur:
        cur.execute("UPDATE jobs SET first_refused_at = %s WHERE id = %s",
                    (NOW - timedelta(days=8), job_id))
    store_conn.commit()
    assert expire_stale(store_conn, cfg, NOW) == 1
    assert _state(store_conn, job_id)[:2] == ("expired", "description_refused")


def test_a_job_specific_refusal_keeps_its_per_job_backoff(store_conn, cfg):
    job_id = _tiered_job(store_conn, cfg)
    describe_next(store_conn, FakePace(), cfg, NOW, get=ScriptedGet(*[response(403)] * 4),
                  sleep=lambda _s: None)
    assert _state(store_conn, job_id)[3:5] == (1, NOW + timedelta(minutes=30))


def test_a_403_job_at_the_head_of_the_queue_does_not_block_the_next_one(store_conn, cfg):
    _tiered_job(store_conn, cfg, "3900000001")
    second = _tiered_job(store_conn, cfg, "3900000002")
    describe_next(store_conn, FakePace(), cfg, NOW, get=ScriptedGet(*[response(403)] * 4),
                  sleep=lambda _s: None)
    get = ScriptedGet(response(200, FRAGMENT))
    assert describe_next(store_conn, FakePace(), cfg, NOW + timedelta(minutes=1), get=get,
                         sleep=lambda _s: None) == "described"
    with store_conn.cursor() as cur:
        cur.execute("SELECT id FROM jobs WHERE stage = 'described'")
        assert cur.fetchone()[0] == second


def test_descriptions_are_overdue_once_the_oldest_waiting_job_is_a_day_old(store_conn, cfg):
    job_id = _tiered_job(store_conn, cfg)
    with store_conn.cursor() as cur:
        cur.execute("UPDATE jobs SET first_seen_at = %s WHERE id = %s", (NOW - timedelta(hours=25), job_id))
    store_conn.commit()
    pace = FakePace()
    describe_next(store_conn, pace, cfg, NOW, get=ScriptedGet(response(200, FRAGMENT)), sleep=lambda _s: None)
    assert pace.overdue_flags == [True]
    fresh = _tiered_job(store_conn, cfg, "3900000002")
    with store_conn.cursor() as cur:
        cur.execute("UPDATE jobs SET first_seen_at = %s WHERE id = %s", (NOW - timedelta(hours=23), fresh))
    store_conn.commit()
    newer = FakePace()
    describe_next(store_conn, newer, cfg, NOW, get=ScriptedGet(response(200, FRAGMENT)), sleep=lambda _s: None)
    assert newer.overdue_flags == [False]


def test_a_job_in_backoff_does_not_make_the_queue_overdue(store_conn, cfg):
    old = _tiered_job(store_conn, cfg, "3900000001")
    with store_conn.cursor() as cur:
        cur.execute("UPDATE jobs SET first_seen_at = %s, next_attempt_at = %s WHERE id = %s",
                    (NOW - timedelta(hours=30), NOW + timedelta(hours=1), old))
    fresh = _tiered_job(store_conn, cfg, "3900000002")
    with store_conn.cursor() as cur:
        cur.execute("UPDATE jobs SET first_seen_at = %s WHERE id = %s", (NOW - timedelta(hours=1), fresh))
    store_conn.commit()
    pace = FakePace()
    describe_next(store_conn, pace, cfg, NOW, get=ScriptedGet(response(200, FRAGMENT)), sleep=lambda _s: None)
    assert pace.overdue_flags == [False]


def test_a_busy_pace_defers_the_job_untouched(store_conn, cfg):
    first = _tiered_job(store_conn, cfg, "3900000001")
    _tiered_job(store_conn, cfg, "3900000002")
    before = _state(store_conn, first)
    get, pace = ScriptedGet(), BusyPace()
    assert run_descriptions(store_conn, pace, cfg, NOW, get=get, sleep=lambda _s: None) == 1
    assert pace.attempts == 1 and get.calls == []
    assert _state(store_conn, first) == before


def test_fetch_description_reports_a_busy_pace_as_deferred():
    assert fetch_description(BusyPace(), "1", get=ScriptedGet(), sleep=lambda _s: None).status == "deferred"


def test_the_shipped_processor_config_has_a_24_hour_description_deadline():
    assert load_processor_config().description_deadline_hours == 24


# -- the queue order: an urgent band, then the tiers take turns --------------------------

def _age(conn, hours_by_job: dict, now: datetime = NOW) -> None:
    """Backdate `first_seen_at` by the given hours."""
    with conn.cursor() as cur:
        for job_id, hours in hours_by_job.items():
            cur.execute("UPDATE jobs SET first_seen_at = %s WHERE id = %s",
                        (now - timedelta(hours=hours), job_id))
    conn.commit()


def _describe_in_order(store_conn, cfg, count: int, start: datetime = NOW) -> list:
    """Run `count` description fetches and return the job ids in the order fetched."""
    fetched: list = []
    for minute in range(count):
        before = set(fetched)
        assert describe_next(store_conn, FakePace(), cfg, start + timedelta(minutes=minute),
                             get=ScriptedGet(response(200, FRAGMENT)), sleep=lambda _s: None) == "described"
        with store_conn.cursor() as cur:
            cur.execute("SELECT id FROM jobs WHERE stage = 'described'")
            picked = {row[0] for row in cur.fetchall()} - before
        assert len(picked) == 1
        fetched.extend(picked)
    return fetched


def test_tiers_take_turns_so_tier_three_and_four_are_not_starved(store_conn, cfg):
    # "ORDER BY tier, first_seen_at" gave tier 3 and tier 4 a turn only once tiers 1 and 2
    # were empty (2026-10-19: 936 tier-3 jobs waiting, the oldest four days old).
    first = _tiered_job(store_conn, cfg, "3900000001")
    second = _tiered_job(store_conn, cfg, "3900000002")
    two = _tiered_job(store_conn, cfg, "3900000003", location="Zurich, Switzerland", tier_hint=2)
    three = _tiered_job(store_conn, cfg, "3900000004", location="Berlin, Germany", tier_hint=3)
    four = _tiered_job(store_conn, cfg, "3900000005", location="London, United Kingdom", tier_hint=4)
    assert _describe_in_order(store_conn, cfg, 5) == [first, two, three, four, second]


def test_tiers_take_turns_when_one_tick_fetches_several_descriptions(store_conn, cfg):
    # One processor tick passes the same `now` to every fetch it makes. The turn must
    # still move on with each fetch, or tiers served in the same tick tie and tier 1/2
    # win every tie, drifting back to "ORDER BY tier".
    two = [_tiered_job(store_conn, cfg, f"390000002{i}", location="Zurich, Switzerland", tier_hint=2)
           for i in range(3)]
    three = [_tiered_job(store_conn, cfg, f"390000003{i}", location="Berlin, Germany", tier_hint=3)
             for i in range(3)]
    four = [_tiered_job(store_conn, cfg, f"390000004{i}", location="London, United Kingdom", tier_hint=4)
            for i in range(3)]
    fetched: list = []
    for _tick in range(3):
        for _fetch in range(3):
            before = set(fetched)
            assert describe_next(store_conn, FakePace(), cfg, NOW, get=ScriptedGet(response(200, FRAGMENT)),
                                 sleep=lambda _s: None) == "described"
            with store_conn.cursor() as cur:
                cur.execute("SELECT id FROM jobs WHERE stage = 'described'")
                fetched.extend({row[0] for row in cur.fetchall()} - before)
    tier_of = {job: tier for tier, jobs in ((2, two), (3, three), (4, four)) for job in jobs}
    # The order is by value rank now, so the old tier interleaving is gone; what this test
    # exists for is that one tick's shared `now` does not collapse the rotation, so every
    # job of every tier is still fetched.
    assert sorted(tier_of[job] for job in fetched) == [2] * 3 + [3] * 3 + [4] * 3


def test_a_job_past_the_urgency_threshold_goes_first_whatever_its_tier(store_conn, cfg):
    _tiered_job(store_conn, cfg, "3900000001")  # the rotation would serve tier 1 first
    old_three = _tiered_job(store_conn, cfg, "3900000003", location="Berlin, Germany", tier_hint=3)
    _age(store_conn, {old_three: 61})
    assert _describe_in_order(store_conn, cfg, 1) == [old_three]


def test_urgent_jobs_take_the_floor_picks_oldest_first_across_tiers(store_conn, cfg):
    # The 60-hour rule is a share: with one pick in eight the first two picks are both floor
    # picks (one per head tier), so both aged jobs go before the fresh one. Exactly at the
    # threshold is not past it.
    four = _tiered_job(store_conn, cfg, "3900000004", location="London, United Kingdom", tier_hint=4)
    three = _tiered_job(store_conn, cfg, "3900000003", location="Berlin, Germany", tier_hint=3)
    one = _tiered_job(store_conn, cfg, "3900000001")
    _age(store_conn, {four: 96, three: 72, one: 60})
    assert _describe_in_order(store_conn, cfg, 3) == [four, three, one]


def test_the_urgent_band_keeps_the_stage_tier_status_and_backoff_filters(store_conn, cfg):
    in_backoff = _tiered_job(store_conn, cfg, "3900000003", location="Berlin, Germany", tier_hint=3)
    described = _tiered_job(store_conn, cfg, "3900000002", location="Zurich, Switzerland", tier_hint=2)
    untiered = record_sighting(store_conn, CardInput(
        source="linkedin", source_job_id="3900000009",
        source_url="https://www.linkedin.com/jobs/view/ai-3900000009", title="AI 3900000009",
        company="Acme", location="Milan, Italy", tier_hint=1), NOW).job_id
    waiting = _tiered_job(store_conn, cfg, "3900000004", location="London, United Kingdom", tier_hint=4)
    with store_conn.cursor() as cur:
        cur.execute("UPDATE jobs SET next_attempt_at = %s WHERE id = %s",
                    (NOW + timedelta(hours=1), in_backoff))
        cur.execute("UPDATE jobs SET description_status = 'ok', description = 'text' WHERE id = %s",
                    (described,))
    store_conn.commit()
    _age(store_conn, {in_backoff: 100, described: 90, untiered: 80})
    assert _describe_in_order(store_conn, cfg, 1) == [waiting]


def test_a_deferred_fetch_does_not_use_up_the_tier_turn(store_conn, cfg):
    # A deferral is no attempt at all (the gate closed between requests), so the tier must
    # keep its place in the rotation instead of being rotated to the back.
    three = _tiered_job(store_conn, cfg, "3900000003", location="Berlin, Germany", tier_hint=3)
    one = _tiered_job(store_conn, cfg, "3900000001")
    assert describe_next(store_conn, BusyPace(), cfg, NOW, get=ScriptedGet(),
                         sleep=lambda _s: None) == "deferred"
    with store_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM kv_state WHERE key LIKE 'description_turn:%%'")
        assert cur.fetchone()[0] == 0
    assert _describe_in_order(store_conn, cfg, 1) == [one]
    assert _state(store_conn, three)[0] == "seen"


def test_the_rotation_state_is_one_kv_state_row_per_tier(store_conn, cfg):
    one = _tiered_job(store_conn, cfg, "3900000001")
    three = _tiered_job(store_conn, cfg, "3900000003", location="Berlin, Germany", tier_hint=3)
    _describe_in_order(store_conn, cfg, 2)
    with store_conn.cursor() as cur:
        cur.execute("SELECT key FROM kv_state WHERE key LIKE 'description_turn:%%' ORDER BY key")
        assert [row[0] for row in cur.fetchall()] == ["description_turn:1", "description_turn:3"]
    assert {_state(store_conn, one)[0], _state(store_conn, three)[0]} == {"described"}


def test_the_shipped_config_puts_a_job_ahead_of_the_rotation_after_60_hours():
    assert load_processor_config().description_urgent_after_hours == 60


@pytest.mark.parametrize("value", [0, -1, "60", 60.5, True, None])
def test_an_invalid_urgency_threshold_is_rejected(tmp_path, value):
    data = json.load(open("config/processor.json"))
    data["description_urgent_after_hours"] = value
    path = tmp_path / "processor.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="description_urgent_after_hours"):
        load_processor_config(str(path))


def test_a_missing_urgency_threshold_defaults_to_60_hours(tmp_path):
    data = json.load(open("config/processor.json"))
    del data["description_urgent_after_hours"]
    path = tmp_path / "processor.json"
    path.write_text(json.dumps(data))
    assert load_processor_config(str(path)).description_urgent_after_hours == 60


def test_the_processor_and_deliver_paces_give_up_after_the_description_budget(cfg):
    pace = linkedin_pace("process", ScheduleConfig(), cfg.description_seconds_per_tick)
    assert pace._options["give_up_after"] == 120


def test_a_job_specific_refusal_also_ends_the_round(store_conn, cfg):
    for n in range(3):
        _tiered_job(store_conn, cfg, f"390000000{n + 1}")
    get = ScriptedGet(*[response(403)] * 4)
    assert run_descriptions(store_conn, FakePace(), cfg, NOW, get=get, sleep=lambda _s: None) == 1
    assert len(get.calls) == 4


# -- the priority class backlog: classified without a deploy script ---------------------

def test_waiting_jobs_without_a_class_are_classified_in_batches(store_conn, cfg):
    for n, location in enumerate(("Italy", "Milan, Italy", "Europe")):
        _tiered_job(store_conn, cfg, f"390000000{n}", title="AI Engineer", location=location)
    with store_conn.cursor() as cur:
        cur.execute("UPDATE jobs SET description_class = NULL")
    store_conn.commit()
    assert classify_waiting(store_conn, cfg, NOW, limit=2) == 2
    assert classify_waiting(store_conn, cfg, NOW) == 1
    with store_conn.cursor() as cur:
        cur.execute("SELECT description_class FROM jobs ORDER BY note_no")
        assert [row[0] for row in cur.fetchall()] == [0, 1, 1]


def test_classification_skips_jobs_that_are_not_waiting(store_conn, cfg):
    described = _tiered_job(store_conn, cfg, "3900000001")
    backoff = _tiered_job(store_conn, cfg, "3900000002")
    with store_conn.cursor() as cur:
        cur.execute("UPDATE jobs SET description_class = NULL, description_status = 'ok', "
                    "description = 'text' WHERE id = %s", (described,))
        cur.execute("UPDATE jobs SET description_class = NULL, next_attempt_at = %s WHERE id = %s",
                    (NOW + timedelta(hours=1), backoff))
    store_conn.commit()
    assert classify_waiting(store_conn, cfg, NOW) == 0
    with store_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs WHERE description_class IS NULL")
        assert cur.fetchone()[0] == 2


# -- the priority class inside the tier turn, and the floor share ------------------------

def _set_picks(conn, n: int, tiers=(1, 2, 3, 4)) -> None:
    """Set the pick counters, i.e. the phase of each tier's floor share."""
    with conn.cursor() as cur:
        for tier in tiers:
            cur.execute("INSERT INTO kv_state (key, value, updated_at) VALUES (%s, "
                        "jsonb_build_object('n', %s), clock_timestamp()) ON CONFLICT (key) "
                        "DO UPDATE SET value = excluded.value",
                        (f"description_pick_count:{tier}", n))
    conn.commit()


def _describe_one(store_conn, cfg, now, done: set):
    """One fetch at `now`; returns the id it described. `done` is updated in place, so a
    loop of fetches works (`_describe_in_order` starts from an empty local set each call)."""
    assert describe_next(store_conn, FakePace(), cfg, now,
                         get=ScriptedGet(response(200, FRAGMENT)), sleep=lambda _s: None) == "described"
    with store_conn.cursor() as cur:
        cur.execute("SELECT id FROM jobs WHERE stage = 'described'")
        picked = {row[0] for row in cur.fetchall()} - done
    assert len(picked) == 1
    done |= picked
    return next(iter(picked))


def test_a_value_pick_takes_a_fresh_high_value_job_ahead_of_an_aged_low_value_one(store_conn, cfg):
    aged = _tiered_job(store_conn, cfg, "3900000001", title="Python Developer", location="Milan, Italy")
    good = _tiered_job(store_conn, cfg, "3900000002", title="AI Engineer", location="Italy")
    _age(store_conn, {aged: 70})
    _set_picks(store_conn, 1)                      # a value pick, not the floor pick
    assert _describe_in_order(store_conn, cfg, 1) == [good]


def test_a_quota_pick_takes_the_oldest_urgent_job_whatever_its_class(store_conn, cfg):
    aged = _tiered_job(store_conn, cfg, "3900000001", title="Python Developer", location="Milan, Italy")
    _tiered_job(store_conn, cfg, "3900000002", title="AI Engineer", location="Italy")
    _age(store_conn, {aged: 70})
    _set_picks(store_conn, 0)                      # the floor pick
    assert _describe_in_order(store_conn, cfg, 1) == [aged]


@pytest.mark.parametrize("arrivals_per_hour", [1, 2])
def test_a_sustained_backlog_meets_the_four_and_eight_day_targets(store_conn, cfg, arrivals_per_hour):
    """The captain's acceptance (2026-10-20), over the real `describe_next`.

    One arrival and one fetch an hour models capacity keeping up with the low-value inflow:
    there the high-value classes must be fetched well inside 4 days in every tier and the
    low-value worst wait must stay under 8 days. Two arrivals and one fetch an hour models the
    evidence's real 2:1 overload: there only the 4-day high-value bound and "low-value jobs keep
    being fetched" are asserted, because no ordering creates capacity (the low-value tail then
    ages out at the 96 h rule, as it does in production). Seeded jobs count towards the 4-day max
    but not towards the p90: they were already 9-48 h old when the clock started."""
    days = 12
    tiers = ((1, "Milan, Italy", 1), (2, "Zurich, Switzerland", 2),
             (3, "Berlin, Germany", 3), (4, "London, United Kingdom", 4))
    pattern = ["high"] * 4 + ["low"] * 20          # a day's 24 arrivals: 4 AI titles, 20 other
    is_high: dict = {}
    tier_of: dict = {}

    def _insert(stamp, slot, tag):
        tier, location, hint = tiers[slot % len(tiers)]
        high = pattern[slot % len(pattern)] == "high"
        job_id = _tiered_job(store_conn, cfg, tag,
                             title="AI Engineer" if high else "Python Developer",
                             location=location, tier_hint=hint)
        with store_conn.cursor() as cur:
            cur.execute("UPDATE jobs SET first_seen_at = %s WHERE id = %s", (stamp, job_id))
        store_conn.commit()
        is_high[job_id], tier_of[job_id] = high, tier
        return job_id

    seeded = [_insert(NOW - timedelta(hours=(120 - n) * 48 // 120), n, f"39000000{n:03d}")
              for n in range(120)]
    seeded_set = set(seeded)
    done: set = set()
    waits = []                                          # (job id, wait)
    for hour in range(24 * days):
        now = NOW + timedelta(hours=hour)
        # The age-out runs every tick in production, so the simulation applies it too.
        with store_conn.cursor() as cur:
            cur.execute("UPDATE jobs SET stage = 'expired', stage_reason = 'aged_out' "
                        "WHERE stage = 'seen' AND tier IN (3, 4) AND description_class >= 2 "
                        "AND first_seen_at < %s", (now - timedelta(hours=96),))
        store_conn.commit()
        for slot in range(arrivals_per_hour):
            _insert(now, hour * arrivals_per_hour + slot, f"3900001{hour:03d}{slot}")
        job_id = _describe_one(store_conn, cfg, now, done)
        with store_conn.cursor() as cur:
            cur.execute("SELECT first_seen_at FROM jobs WHERE id = %s", (job_id,))
            seen = cur.fetchone()[0]
        waits.append((job_id, now - seen))
    arrivals_high = sorted(wait for job, wait in waits if is_high[job] and job not in seeded_set)
    all_high = [wait for job, wait in waits if is_high[job]]
    low = [wait for job, wait in waits if not is_high[job]]
    assert len(arrivals_high) >= 10 and low
    assert arrivals_high[int(len(arrivals_high) * 0.9)] < timedelta(days=1)     # arrivals-only p90
    per_tier_high = [[wait for job, wait in waits if is_high[job] and tier_of[job] == tier]
                     for tier, _loc, _hint in tiers]
    assert all(tier_waits and max(tier_waits) < timedelta(days=4) for tier_waits in per_tier_high)
    assert max(all_high) < timedelta(days=4)
    assert low                                              # low-value jobs keep being fetched
    # a floor-level guard only; the per-tier max(low) below is the real 8-day check
    assert len(low) >= len(waits) / cfg.fetch_order.quota_one_in
    if arrivals_per_hour == 1:                              # capacity keeps up: the 8-day target holds
        assert max(low) < timedelta(days=8)


def test_a_waiting_job_without_a_class_is_still_reachable(store_conn, cfg):
    blank = _tiered_job(store_conn, cfg, "3900000001", title="AI Engineer", location="Italy")
    other = _tiered_job(store_conn, cfg, "3900000002", title="AI Engineer", location="Italy")
    with store_conn.cursor() as cur:
        cur.execute("UPDATE jobs SET description_class = NULL WHERE id = %s", (blank,))
    store_conn.commit()
    _set_picks(store_conn, 1)                      # value picks; NULL sorts last, never unreachable
    assert _describe_in_order(store_conn, cfg, 2) == [other, blank]


def test_a_value_pick_takes_the_best_class_across_all_tiers(store_conn, cfg):
    aged_low = _tiered_job(store_conn, cfg, "3900000001", title="Python Developer",
                           location="London, United Kingdom", tier_hint=4)
    best = _tiered_job(store_conn, cfg, "3900000002", title="AI Engineer", location="Italy",
                       tier_hint=3)
    good = _tiered_job(store_conn, cfg, "3900000003", title="AI Engineer",
                       location="Milan, Italy", tier_hint=1)
    _age(store_conn, {aged_low: 80, good: 40})
    _set_picks(store_conn, 1)                      # a value pick, not the floor pick
    assert _describe_in_order(store_conn, cfg, 2) == [best, good]


def test_the_floor_pick_takes_the_oldest_important_job_of_the_head_tier(store_conn, cfg):
    low = _tiered_job(store_conn, cfg, "3900000001", title="Python Developer",
                      location="Berlin, Germany", tier_hint=3)
    important = _tiered_job(store_conn, cfg, "3900000002", title="AI Engineer",
                            location="Milan, Italy", tier_hint=3)
    _age(store_conn, {low: 50, important: 10})
    _set_picks(store_conn, 0)                      # the floor pick
    assert _describe_in_order(store_conn, cfg, 1) == [important]


def test_the_floor_pick_falls_back_to_the_value_order_without_important_work(store_conn, cfg):
    low = _tiered_job(store_conn, cfg, "3900000001", title="Python Developer",
                      location="Berlin, Germany", tier_hint=3)
    best = _tiered_job(store_conn, cfg, "3900000002", title="AI Engineer",
                       location="London, United Kingdom", tier_hint=4)
    _age(store_conn, {low: 50})                     # 50 h, so still short of the urgent band
    with store_conn.cursor() as cur:                # tier 4 served last, so tier 3 is the head
        cur.execute("INSERT INTO kv_state (key, value) VALUES ('description_turn:4', "
                    "jsonb_build_object('at', now())) ON CONFLICT (key) "
                    "DO UPDATE SET value = EXCLUDED.value")
    store_conn.commit()
    _set_picks(store_conn, 0)
    assert _describe_in_order(store_conn, cfg, 1) == [best]


def test_one_pick_in_eight_is_the_floor(store_conn, cfg):
    # Picks 4 and 5 of the cycle: value picks under 1-in-8 (the old 1-in-4 code would make
    # pick 4 the oldest-first floor and take `older`). This test fails on the old code.
    older = _tiered_job(store_conn, cfg, "3900000001", title="AI Engineer",
                        location="Milan, Italy")          # ai_city
    first = _tiered_job(store_conn, cfg, "3900000002", title="AI Engineer",
                        location="Italy")                 # ai_country
    second = _tiered_job(store_conn, cfg, "3900000003", title="AI Engineer",
                         location="Italy")
    _age(store_conn, {older: 10})
    _set_picks(store_conn, 3)
    assert _describe_in_order(store_conn, cfg, 2) == [first, second]


def test_the_counter_eight_pick_is_the_floor_and_takes_the_oldest_important_job(store_conn, cfg):
    older = _tiered_job(store_conn, cfg, "3900000001", title="AI Engineer",
                        location="Milan, Italy")
    first = _tiered_job(store_conn, cfg, "3900000002", title="AI Engineer", location="Italy")
    _tiered_job(store_conn, cfg, "3900000003", title="AI Engineer", location="Italy")
    _age(store_conn, {older: 10})
    _set_picks(store_conn, 7)      # the counter-8 pick is the floor: oldest important, not the better rank
    assert _describe_in_order(store_conn, cfg, 2) == [first, older]


def test_no_tier_starves_of_its_important_work(store_conn, cfg):
    tiers = ((1, "Milan, Italy", 1), (2, "Zurich, Switzerland", 2),
             (3, "Berlin, Germany", 3), (4, "London, United Kingdom", 4))
    floors = {tier: _tiered_job(store_conn, cfg, f"390000000{tier}", title="AI Coordinator",
                                location=location, tier_hint=hint)
              for tier, location, hint in tiers}
    done: set = set()
    fetched = []
    for round_no in range(4 * cfg.fetch_order.quota_one_in):
        for tier, location, hint in tiers:
            _tiered_job(store_conn, cfg, f"39000001{round_no}{tier}", title="AI Engineer",
                        location=location, tier_hint=hint)
        fetched.append(_describe_one(store_conn, cfg, NOW + timedelta(minutes=round_no), done))
    assert set(floors.values()) <= set(fetched)


def test_a_head_tier_without_important_work_does_not_block_the_floor_of_the_others(store_conn, cfg):
    # Tier 2 keeps only a non-important job, so it can never serve its own floor. Without
    # recording the head tier's turn on a floor attempt it stays head for ever (a never
    # served tier sorts first), and tier 4's floor job below is never reached.
    _tiered_job(store_conn, cfg, "3900000002", title="Python Developer",
                location="Zurich, Switzerland", tier_hint=2)
    starved = _tiered_job(store_conn, cfg, "3900000004", title="AI Coordinator",
                          location="London, United Kingdom", tier_hint=4)
    done, fetched = set(), []
    for round_no in range(4 * cfg.fetch_order.quota_one_in):
        _tiered_job(store_conn, cfg, f"39000001{round_no:02d}", title="AI Engineer",
                    location="Italy", tier_hint=1)
        fetched.append(_describe_one(store_conn, cfg, NOW + timedelta(minutes=round_no), done))
    assert starved in fetched


def test_disabled_reproduces_the_current_order(store_conn, cfg):
    off = replace(cfg, fetch_order=replace(cfg.fetch_order, enabled=False))
    old = _tiered_job(store_conn, off, "3900000001", title="Python Developer", location="Milan, Italy")
    fresh = _tiered_job(store_conn, off, "3900000002", title="AI Engineer", location="Italy")
    _tiered_job(store_conn, off, "3900000003")
    _age(store_conn, {old: 5})
    assert _describe_in_order(store_conn, off, 2) == [old, fresh]


def test_a_quota_of_one_makes_every_pick_the_floor(store_conn, cfg):
    one = replace(cfg, fetch_order=replace(cfg.fetch_order, quota_one_in=1))
    old = _tiered_job(store_conn, one, "3900000001", title="Python Developer", location="Milan, Italy")
    fresh = _tiered_job(store_conn, one, "3900000002", title="AI Engineer", location="Italy")
    _age(store_conn, {old: 5})
    # The floor pick is now "oldest important" of the head tier, and the old job is
    # other_city, so the fresh AI job comes first.
    assert _describe_in_order(store_conn, one, 2) == [fresh, old]


def test_a_deferred_fetch_consumes_no_pick_and_no_turn(store_conn, cfg):
    _tiered_job(store_conn, cfg)
    assert describe_next(store_conn, BusyPace(), cfg, NOW, get=ScriptedGet(),
                         sleep=lambda _s: None) == "deferred"
    with store_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM kv_state WHERE key LIKE 'description_pick_count:%%'")
        assert cur.fetchone()[0] == 0


def test_the_pick_counter_is_one_kv_state_row_per_tier(store_conn, cfg):
    for n in range(5):
        _tiered_job(store_conn, cfg, f"390000000{n}")
    _describe_in_order(store_conn, cfg, 5)          # one tier waiting
    with store_conn.cursor() as cur:
        cur.execute("SELECT key, value->>'n' FROM kv_state WHERE key LIKE 'description_pick_count:%%'")
        assert cur.fetchall() == [("description_pick_count:1", "5")]



# -- the pick stays index-backed and cheap at a 3,000-row waiting queue ------------------

def test_the_description_pick_stays_index_backed_on_a_three_thousand_row_queue(store_conn, cfg):
    with store_conn.cursor() as cur:
        cur.execute("""INSERT INTO jobs (canonical_url, title, company, company_norm, title_norm,
                       location_raw, first_seen_at, next_attempt_at, tier, stage, description_status)
                       SELECT 'https://l/w'||g, 'AI Engineer '||g, 'Acme', 'acme', 'ai engineer',
                              CASE WHEN g %% 9 = 0 THEN 'Italy' ELSE 'Milan, Italy' END,
                              %s - (g * 3 || ' minutes')::interval, %s - interval '1 day',
                              1 + (g %% 4), 'seen', 'pending'
                       FROM generate_series(1, 3000) g""", (NOW, NOW))
        cur.execute("""INSERT INTO jobs (canonical_url, title, company, company_norm, title_norm,
                       location_raw, first_seen_at, tier, stage, description_status)
                       SELECT 'https://l/x'||g, 'AI Engineer '||g, 'Acme', 'acme', 'ai engineer',
                              'Italy', %s - interval '10 days', 1, 'scored', 'ok'
                       FROM generate_series(1, 30000) g""", (NOW,))
        cur.execute("UPDATE jobs SET description_class = abs(hashtext(id::text)) % 4 WHERE stage = 'seen'")
        cur.execute("ANALYZE jobs")
        # the dataset must actually be the one this test claims: 3,000 waiting, 1,800 past the floor
        cur.execute("SELECT count(*) FROM jobs WHERE " + describe._WAITING, (NOW,))
        assert cur.fetchone()[0] == 3000
        cur.execute("SELECT count(*) FROM jobs WHERE " + describe._WAITING + " AND first_seen_at < %s",
                    (NOW, NOW - timedelta(hours=cfg.description_urgent_after_hours)))
        assert cur.fetchone()[0] == 1800
        for label, sql, params in describe.pick_queries(cfg, NOW):
            cur.execute("EXPLAIN (ANALYZE) " + sql, params)
            plan = "\n".join(row[0] for row in cur.fetchall())
            assert "Seq Scan on jobs" not in plan
            # which of the two partial indexes the planner picks depends on statistics; it must be one
            assert "jobs_description_queue_idx" in plan or "jobs_next_attempt_idx" in plan
            if label == "floor":
                # One tier of the four: about 750 of the 3,000 waiting rows.
                assert any(int(n) > 500
                           for n in re.findall(r"actual time=[\d.]+\.[\d.]+ rows=(\d+)", plan))
            elif label != "urgent":   # the urgent query stops at the oldest row: rows=1 is correct
                assert any(int(n) > 1000
                           for n in re.findall(r"actual time=[\d.]+\.[\d.]+ rows=(\d+)", plan))


def test_a_403_description_records_the_block():
    pace = FakePace()
    result = fetch_description(pace, "3900000001", get=ScriptedGet(*[response(403)] * 4),
                               sleep=lambda _s: None)
    assert result.status == "refused"
    assert pace.block_reasons == ["http_403"] * 4


def test_a_999_description_records_the_block():
    pace = FakePace()
    result = fetch_description(pace, "3900000001", get=ScriptedGet(response(999)),
                               sleep=lambda _s: None)
    assert result.status == "throttled"
    assert pace.block_reasons == ["http_999"]


def test_a_connection_error_description_records_the_block():
    pace = FakePace()
    result = fetch_description(pace, "3900000001",
                               get=ScriptedGet(*[requests.ConnectionError("down")] * 4),
                               sleep=lambda _s: None)
    assert result.status == "refused"
    assert pace.block_reasons == ["connection"] * 4


def test_a_timeout_description_does_not_record_a_block():
    pace = FakePace()
    result = fetch_description(pace, "3900000001",
                               get=ScriptedGet(*[requests.Timeout("slow")] * 4),
                               sleep=lambda _s: None)
    assert result.status == "refused"
    assert pace.block_reasons == []


def test_a_captcha_description_is_deferred_not_failed():
    pace = FakePace()
    result = fetch_description(pace, "3900000001",
                               get=ScriptedGet(response(200, "<html>/checkpoint/challenge</html>")),
                               sleep=lambda _s: None)
    assert result.status == "deferred"
    assert pace.block_reasons == ["captcha"]


def test_a_404_description_is_gone_without_a_block():
    pace = FakePace()
    result = fetch_description(pace, "3900000001", get=ScriptedGet(response(404)),
                               sleep=lambda _s: None)
    assert result.status == "gone" and pace.block_reasons == []


def test_a_valid_description_containing_the_word_challenge_is_not_a_captcha():
    pace = FakePace()
    body = FRAGMENT.replace("cloud infrastructure", "cloud infrastructure and challenge")
    result = fetch_description(pace, "3900000001", get=ScriptedGet(response(200, body)),
                               sleep=lambda _s: None)
    assert result.status == "ok" and pace.block_reasons == []
