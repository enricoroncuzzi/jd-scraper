import pytest
import requests

from src.linkedin.search import (
    HEADERS, RETRY_WAITS, SEARCH_URL, SearchFailed, SearchThrottled, fetch_page,
)
from tests.linkedin_fakes import FakePace, ScriptedGet, ids, page_html, response


def _fetch(get, pace=None, sleeps=None):
    pace = pace or FakePace()
    sleeps = sleeps if sleeps is not None else []
    page = fetch_page(pace, role="AI Engineer", area="Italy", f_tpr_value="r21600", start=20,
                      get=get, sleep=sleeps.append)
    return page, pace, sleeps


def test_a_200_page_is_parsed_and_counts_as_clean():
    get = ScriptedGet(response(200, page_html(*ids(0, 3))))
    page, pace, sleeps = _fetch(get)
    assert [c.job_id for c in page.cards] == ids(0, 3)
    assert (page.throttled, page.end) == (False, False)
    assert pace.events == [("search", "clean")] and sleeps == []


def test_only_the_four_honoured_parameters_are_sent():
    get = ScriptedGet(response(200, page_html()))
    _fetch(get)
    assert get.calls == [{"keywords": "AI Engineer", "location": "Italy", "f_TPR": "r21600", "start": 20}]
    assert SEARCH_URL.endswith("/seeMoreJobPostings/search") and "User-Agent" in HEADERS


@pytest.mark.parametrize("status", [429, 999])
def test_a_throttle_returns_at_once_as_one_refusal(status):
    get, sleeps, pace = ScriptedGet(response(status)), [], FakePace()
    with pytest.raises(SearchThrottled):
        _fetch(get, pace=pace, sleeps=sleeps)
    assert len(get.calls) == 1 and sleeps == []
    assert pace.events == [("search", "throttled")]


def test_retry_after_is_handed_to_the_pace():
    pace = FakePace()
    with pytest.raises(SearchThrottled):
        _fetch(ScriptedGet(response(429, headers={"Retry-After": "90"})), pace=pace)
    assert pace.retry_afters == [90.0]


def test_an_empty_page_after_a_refusal_counts_as_a_refusal():
    pace = FakePace()
    page = fetch_page(pace, role="AI Engineer", area="Italy", f_tpr_value="r3600", start=10,
                      distrust_empty=True, get=ScriptedGet(response(200, page_html())), sleep=lambda _s: None)
    assert page.throttled and page.cards == [] and pace.events == [("search", "throttled")]


def test_an_empty_page_is_trusted_when_nothing_was_refused():
    pace = FakePace()
    page = fetch_page(pace, role="AI Engineer", area="Italy", f_tpr_value="r3600", start=10,
                      get=ScriptedGet(response(200, page_html())), sleep=lambda _s: None)
    assert not page.throttled and pace.events == [("search", "clean")]


def test_503_is_retried_without_slowing_the_pace():
    get = ScriptedGet(response(503), response(200, page_html(*ids(0, 1))))
    page, pace, _ = _fetch(get)
    assert not page.throttled
    assert pace.events == [("search", "neutral"), ("search", "clean")]


def test_a_400_means_the_offset_is_past_the_end():
    page, _, _ = _fetch(ScriptedGet(response(400)))
    assert page.end and page.cards == []


def test_a_non_retryable_status_fails_at_once():
    get = ScriptedGet(response(403))
    with pytest.raises(SearchFailed, match="403"):
        _fetch(get)
    assert len(get.calls) == 1


def test_a_network_error_that_never_recovers_fails_without_claiming_a_throttle():
    get = ScriptedGet(*[requests.ConnectionError("down") for _ in range(4)])
    with pytest.raises(SearchFailed):
        _fetch(get)


def test_a_neutral_retry_beats_before_every_attempt(pulses):
    get = ScriptedGet(response(503), response(503), response(200, page_html(*ids(0, 1))))
    _fetch(get)
    assert [call["attempt"] for call in pulses] == [1, 2, 3]
    assert pulses[0] == {"search": "AI Engineer / Italy", "start": 20, "attempt": 1}
