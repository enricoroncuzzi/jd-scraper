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


def test_a_429_is_retried_after_five_seconds_and_marks_the_page_throttled():
    get = ScriptedGet(response(429), response(200, page_html(*ids(0, 2))))
    page, pace, sleeps = _fetch(get)
    assert page.throttled and len(page.cards) == 2
    assert pace.events == [("search", "throttled"), ("search", "clean")]
    assert sleeps == [5]


def test_999_is_treated_like_429():
    get = ScriptedGet(response(999), response(200, page_html(*ids(0, 1))))
    page, pace, _ = _fetch(get)
    assert page.throttled and pace.events[0] == ("search", "throttled")


def test_retries_wait_5_15_45_then_the_request_is_deferred():
    get = ScriptedGet(*[response(429) for _ in range(4)])
    pace, sleeps = FakePace(), []
    with pytest.raises(SearchThrottled):
        _fetch(get, pace, sleeps)
    assert sleeps == list(RETRY_WAITS) == [5, 15, 45]
    assert [outcome for _, outcome in pace.events] == ["throttled"] + ["throttled_again"] * 3
    assert len(get.calls) == 4


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


def test_a_refused_and_retried_page_beats_before_every_attempt(pulses):
    get = ScriptedGet(response(429), response(429), response(200, page_html(*ids(0, 1))))
    _fetch(get)
    assert [call["attempt"] for call in pulses] == [1, 2, 3]
    assert pulses[0] == {"search": "AI Engineer / Italy", "start": 20, "attempt": 1}
