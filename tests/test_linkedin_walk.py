from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest


from src.linkedin.catalog import CollectorConfig, SliceSpec, sync_slices
from src.linkedin.walk import WalkResult, claim_due_slice, finish_walk, walk_slice
from tests.linkedin_fakes import FakePace, ScriptedGet, ids, page_html, response

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
H = 3600


def _cfg(**overrides):
    values = dict(due_hours={1: 4}, max_pages_per_slice=90, known_pages_to_stop=3,
                  window_margin_hours=2)
    values.update(overrides)
    return CollectorConfig(**values)


def _seed_slice(conn, **overrides):
    """A real `slices` row (sightings reference it) as the SliceRow a walk receives."""
    sync_slices(conn, [SliceSpec("linkedin", "AI Engineer", "Italy", 1, 4 * H)], NOW)
    conn.commit()
    return replace(claim_due_slice(conn, NOW), **overrides)


def _count(conn, table="sightings"):
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        return cur.fetchone()[0]


def _walk(conn, get, cfg=None, **row_overrides):
    sleeps = []
    row = _seed_slice(conn, **row_overrides)
    result = walk_slice(conn, FakePace(), row, cfg or _cfg(), NOW, get=get, sleep=sleeps.append)
    return result, sleeps


def test_a_walk_records_every_card_and_stops_at_the_empty_page(store_conn):
    get = ScriptedGet(response(200, page_html(*ids(0, 10))), response(200, page_html(*ids(10, 10))),
                      response(200, page_html(*ids(20, 3))), response(200, page_html()))
    result, _ = _walk(store_conn, get)
    assert (result.pages, result.cards_seen, result.new_jobs) == (4, 23, 23)
    assert result.stop_reason == "end_of_results" and result.finished
    assert result.window == 86400
    assert _count(store_conn) == 23 == _count(store_conn, "jobs")
    assert [call["start"] for call in get.calls] == [0, 10, 20, 23]
    assert {call["f_TPR"] for call in get.calls} == {"r86400"}


def test_new_jobs_carry_the_slice_tier_as_their_hint(store_conn):
    _walk(store_conn, ScriptedGet(response(200, page_html(*ids(0, 2))), response(200, page_html())))
    with store_conn.cursor() as cur:
        cur.execute("SELECT DISTINCT tier_hint FROM jobs")
        assert cur.fetchall() == [(1,)]


def test_start_advances_by_the_cards_actually_returned(store_conn):
    get = ScriptedGet(response(200, page_html(*ids(0, 7))), response(200, page_html(*ids(7, 4))),
                      response(200, page_html()))
    _walk(store_conn, get)
    assert [call["start"] for call in get.calls] == [0, 7, 11]


def test_the_window_reaches_back_to_the_last_walk_plus_the_margin(store_conn):
    get = ScriptedGet(response(200, page_html()))
    result, _ = _walk(store_conn, get, last_walked_at=NOW - timedelta(hours=4))
    assert result.window == 6 * H and get.calls[0]["f_TPR"] == "r21600"


def test_three_consecutive_known_full_pages_stop_the_walk(store_conn):
    known = [ids(0, 10), ids(10, 10), ids(20, 10)]
    _walk(store_conn, ScriptedGet(*[response(200, page_html(*p)) for p in known], response(200, page_html())))
    get = ScriptedGet(*[response(200, page_html(*p)) for p in known], response(200, page_html(*ids(30, 10))))
    result, _ = _walk(store_conn, get)
    assert result.stop_reason == "known_page" and result.pages == 3 and result.new_jobs == 0
    assert len(get.queue) == 1  # the fourth page was never requested


def test_one_known_page_does_not_stop_the_walk(store_conn):
    _walk(store_conn, ScriptedGet(response(200, page_html(*ids(0, 10))), response(200, page_html())))
    get = ScriptedGet(response(200, page_html(*ids(0, 10))), response(200, page_html(*ids(10, 10))),
                      response(200, page_html()))
    result, _ = _walk(store_conn, get)
    assert result.new_jobs == 10 and result.stop_reason == "end_of_results"


def test_reaching_the_page_cap_with_a_full_last_page_is_cap_hit(store_conn):
    get = ScriptedGet(response(200, page_html(*ids(0, 10))), response(200, page_html(*ids(10, 10))))
    result, _ = _walk(store_conn, get, cfg=_cfg(max_pages_per_slice=2))
    assert result.stop_reason == "cap_hit" and result.pages == 2 and result.next_start == 20


def test_a_repeated_page_ends_the_walk(store_conn):
    get = ScriptedGet(response(200, page_html(*ids(0, 10))), response(200, page_html(*ids(0, 10))))
    result, _ = _walk(store_conn, get)
    assert result.stop_reason == "end_of_results" and result.cards_seen == 10


def test_a_400_after_the_first_page_keeps_what_was_fetched(store_conn):
    get = ScriptedGet(response(200, page_html(*ids(0, 10))), response(400))
    result, _ = _walk(store_conn, get)
    assert result.stop_reason == "end_of_results" and _count(store_conn) == 10


def test_three_refusals_in_a_row_defer_the_slice_and_keep_the_resume_point(store_conn):
    get = ScriptedGet(response(200, page_html(*ids(0, 10))), response(429), response(429), response(429))
    pace = FakePace()
    row = _seed_slice(store_conn)
    result = walk_slice(store_conn, pace, row, _cfg(), NOW, get=get, sleep=lambda _s: None)
    assert (result.stop_reason, result.pages, result.next_start, result.finished) == ("rate_limited", 1, 10, False)
    assert [c["start"] for c in get.calls] == [0, 10, 10, 10]
    assert pace.events == [("search", "clean")] + [("search", "throttled")] * 3


def test_a_refusal_then_a_clean_page_carries_on_at_the_same_offset(store_conn):
    get = ScriptedGet(response(200, page_html(*ids(0, 10))), response(429),
                      response(200, page_html(*ids(10, 4))), response(200, page_html()))
    result, _ = _walk(store_conn, get)
    assert result.stop_reason == "end_of_results" and result.cards_seen == 14
    assert [c["start"] for c in get.calls] == [0, 10, 10, 14]


def test_refusals_that_are_not_consecutive_do_not_defer(store_conn):
    # The last refusal before the final empty page is followed by a clean page first, so the
    # empty page is trusted (D-h) and ends the walk normally.
    get = ScriptedGet(response(200, page_html(*ids(0, 10))), response(429), response(429),
                      response(200, page_html(*ids(10, 10))), response(429), response(429),
                      response(200, page_html(*ids(20, 5))), response(200, page_html()))
    result, _ = _walk(store_conn, get)
    assert result.finished and result.stop_reason == "end_of_results"
    assert [c["start"] for c in get.calls] == [0, 10, 10, 10, 20, 20, 20, 25]
    assert (result.pages, result.cards_seen) == (4, 25)


def test_an_empty_page_right_after_a_refusal_is_asked_again(store_conn):
    get = ScriptedGet(response(200, page_html(*ids(0, 10))), response(429), response(200, page_html()),
                      response(200, page_html(*ids(10, 3))), response(200, page_html()))
    result, _ = _walk(store_conn, get)
    assert result.stop_reason == "end_of_results" and result.cards_seen == 13


def test_a_refusal_then_two_empty_pages_is_an_unfinished_throttled_empty_walk(store_conn):
    get = ScriptedGet(response(200, page_html(*ids(0, 10))), response(429),
                      response(200, page_html()), response(200, page_html()))
    result, _ = _walk(store_conn, get)
    assert (result.stop_reason, result.finished, result.next_start) == ("throttled_empty", False, 10)
    assert result.pages == 1  # refused responses are not pages


def test_an_unfinished_slice_resumes_where_it_stopped(store_conn):
    get = ScriptedGet(response(200, page_html()))
    _walk(store_conn, get, resume_start=40)
    assert get.calls[0]["start"] == 40


def test_a_crash_after_a_page_persists_the_resume_offset(store_conn):
    class Boom(RuntimeError):
        pass
    get = ScriptedGet(response(200, page_html(*ids(0, 10))), response(200, page_html(*ids(10, 10))),
                      Boom("process died"))
    row = _seed_slice(store_conn)
    with pytest.raises(Boom):
        walk_slice(store_conn, FakePace(), row, _cfg(), NOW, get=get, sleep=lambda _s: None)
    with store_conn.cursor() as cur:
        cur.execute("SELECT resume_start FROM slices WHERE id = %s", (row.id,))
        assert cur.fetchone()[0] == 20
    # the next walk resumes at the persisted offset instead of the start
    get2 = ScriptedGet(response(200, page_html()))
    walk_slice(store_conn, FakePace(), replace(row, resume_start=20), _cfg(), NOW,
               get=get2, sleep=lambda _s: None)
    assert get2.calls[0]["start"] == 20


def _slice_state(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT last_walked_at, stop_reason, interval_seconds, consecutive_empty, "
                    "resume_start, due_at, pages_walked FROM slices")
        return cur.fetchone()


def test_claim_due_slice_orders_by_tier_then_due_time(store_conn):
    sync_slices(store_conn, [SliceSpec("linkedin", "AI Engineer", "Germany", 3, 12 * H),
                             SliceSpec("linkedin", "AI Engineer", "Italy", 1, 4 * H)], NOW)
    store_conn.commit()
    assert claim_due_slice(store_conn, NOW).area == "Italy"
    with store_conn.cursor() as cur:
        cur.execute("UPDATE slices SET due_at = %s", (NOW + timedelta(hours=1),))
    assert claim_due_slice(store_conn, NOW) is None


def test_finishing_a_walk_schedules_the_next_one(store_conn):
    row = _seed_slice(store_conn)
    result = WalkResult(pages=3, cards_seen=23, new_jobs=23, stop_reason="end_of_results",
                        window=86400, next_start=23)
    finish_walk(store_conn, row, result, NOW, NOW + timedelta(minutes=2))
    last_walked, reason, interval, empty, resume, due, pages = _slice_state(store_conn)
    assert last_walked == NOW and reason == "end_of_results" and pages == 3
    assert (interval, empty, resume) == (4 * H, 0, 0)
    assert due == NOW + timedelta(minutes=2, seconds=4 * H)


def test_cap_hit_halves_the_interval_so_the_next_window_is_smaller(store_conn):
    row = _seed_slice(store_conn)
    result = WalkResult(90, 900, 800, "cap_hit", 86400, 900)
    finish_walk(store_conn, row, result, NOW, NOW)
    assert _slice_state(store_conn)[2] == 2 * H


def test_an_unfinished_walk_keeps_last_walked_at_and_returns_in_30_minutes(store_conn):
    row = _seed_slice(store_conn)
    result = WalkResult(5, 50, 50, "rate_limited", 86400, 50)
    finish_walk(store_conn, row, result, NOW, NOW)
    last_walked, reason, interval, _empty, resume, due, _pages = _slice_state(store_conn)
    assert last_walked is None and resume == 50 and reason == "rate_limited"
    assert interval == 4 * H and due == NOW + timedelta(minutes=30)


def test_three_empty_walks_in_a_row_move_the_slice_to_daily(store_conn):
    row = _seed_slice(store_conn)
    empty = WalkResult(1, 0, 0, "end_of_results", 86400, 0)
    for _ in range(3):
        state = _slice_state(store_conn)
        row = replace(row, consecutive_empty=state[3], interval_seconds=state[2])
        finish_walk(store_conn, row, empty, NOW, NOW)
    state = _slice_state(store_conn)
    assert state[3] == 3 and state[2] == 24 * H


def test_claim_due_slice_only_returns_slices_of_the_asked_source(store_conn):
    sync_slices(store_conn, [SliceSpec("linkedin", "AI Engineer", "Italy", 1, 4 * H)], NOW)
    sync_slices(store_conn, [SliceSpec("freehire", "ai_engineering", "countries=it", 1, 24 * H)], NOW, "freehire")
    store_conn.commit()
    assert claim_due_slice(store_conn, NOW).area == "Italy"
    assert claim_due_slice(store_conn, NOW, "freehire").area == "countries=it"
    assert claim_due_slice(store_conn, NOW, "himalayas") is None


def test_a_walk_beats_before_every_page_it_fetches(store_conn, pulses):
    get = ScriptedGet(response(200, page_html(*ids(0, 10))), response(200, page_html(*ids(10, 10))),
                      response(200, page_html()))
    result, _ = _walk(store_conn, get)
    assert result.pages == 3
    walking = [call for call in pulses if "pages" in call]
    assert [call["pages"] for call in walking] == [0, 1, 2]
    assert walking[0]["slice"] == "AI Engineer / Italy" and walking[2]["new_jobs"] == 20


def test_every_walk_is_logged_with_its_requests_hits_and_new_jobs(store_conn):
    row = _seed_slice(store_conn)
    finish_walk(store_conn, row, WalkResult(3, 23, 20, "end_of_results", 86400, 23), NOW, NOW)
    finish_walk(store_conn, row, WalkResult(2, 12, 0, "rate_limited", 86400, 12), NOW + timedelta(hours=4),
                NOW + timedelta(hours=4))
    with store_conn.cursor() as cur:
        cur.execute("SELECT walked_at, pages, cards_seen, new_jobs, stop_reason, window_seconds "
                    "FROM slice_walks WHERE slice_id = %s ORDER BY walked_at", (row.id,))
        assert cur.fetchall() == [(NOW, 3, 23, 20, "end_of_results", 86400),
                                  (NOW + timedelta(hours=4), 2, 12, 0, "rate_limited", 86400)]
