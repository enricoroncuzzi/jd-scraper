from datetime import date, datetime, timezone

from src.jobstore.source_requests import (
    NO_STATUS, OK_WITHOUT_STATUS, daily_requests, record_request,
)
from src.jobstore.sightings import CardInput, record_sighting

NOON = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


def _event(conn, at, outcome, status=None):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO pace_events (at, service, kind, outcome, status) "
                    "VALUES (%s, 'collect-linkedin', 'search', %s, %s)", (at, outcome, status))
    conn.commit()


def _sighting(conn, source, source_job_id, at):
    record_sighting(conn, CardInput(source=source, source_job_id=source_job_id,
                                    source_url=f"https://example/{source_job_id}",
                                    title="t", company="c"), at)
    conn.commit()


def test_a_request_belongs_to_its_rome_day(store_conn):
    # 21:30 UTC in summer is 23:30 Rome the same day; 22:30 UTC is 00:30 Rome the next.
    _event(store_conn, datetime(2026, 10, 23, 21, 30, tzinfo=timezone.utc), "ok", 200)
    _event(store_conn, datetime(2026, 10, 23, 21, 45, tzinfo=timezone.utc), "refused", 429)
    _event(store_conn, datetime(2026, 10, 23, 22, 30, tzinfo=timezone.utc), "neutral")
    record_request(store_conn, "freehire", datetime(2026, 10, 23, 22, 30, tzinfo=timezone.utc), 200)
    store_conn.commit()
    rows = {(r.source, r.day.isoformat()): r
            for r in daily_requests(store_conn, date(2026, 10, 23), date(2026, 10, 24))}
    first = rows[("linkedin", "2026-10-23")]
    assert (first.sent, first.ok, first.refused, first.errors) == (2, 1, 1, 0)
    assert rows[("linkedin", "2026-10-24")].sent == 1
    assert rows[("linkedin", "2026-10-24")].errors == 1
    assert rows[("freehire", "2026-10-24")].ok == 1
    assert rows[("freehire", "2026-10-23")].sent == 0


def test_the_fall_back_day_is_25_hours_long(store_conn):
    # 2026-10-25 is 25 hours long in Rome. 22:30 UTC is 23:30 CET, still the 25th;
    # 23:30 UTC is 00:30 CET on the 26th. A fixed UTC+2 offset gets the first one wrong.
    _event(store_conn, datetime(2026, 10, 25, 22, 30, tzinfo=timezone.utc), "ok", 200)
    _event(store_conn, datetime(2026, 10, 25, 23, 30, tzinfo=timezone.utc), "ok", 200)
    rows = {(r.source, r.day.isoformat()): r
            for r in daily_requests(store_conn, date(2026, 10, 25), date(2026, 10, 26))}
    assert rows[("linkedin", "2026-10-25")].sent == 1
    assert rows[("linkedin", "2026-10-26")].sent == 1


def test_a_day_with_no_requests_still_gets_a_row_of_zeros(store_conn):
    rows = daily_requests(store_conn, date(2026, 10, 20), date(2026, 10, 20))
    assert [(r.source, r.sent, r.ok, r.refused, r.errors, r.new_jobs) for r in rows] == [
        ("linkedin", 0, 0, 0, 0, 0), ("freehire", 0, 0, 0, 0, 0), ("himalayas", 0, 0, 0, 0, 0)]
    assert [r.codes_text for r in rows] == ["-", "-", "-"]


def test_new_jobs_are_the_sightings_first_seen_that_day(store_conn):
    _sighting(store_conn, "himalayas", "h1", datetime(2026, 10, 23, 8, 0, tzinfo=timezone.utc))
    _sighting(store_conn, "himalayas", "h2", datetime(2026, 10, 23, 8, 5, tzinfo=timezone.utc))
    _sighting(store_conn, "linkedin", "l1", datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc))
    rows = {(r.source, r.day.isoformat()): r
            for r in daily_requests(store_conn, date(2026, 10, 22), date(2026, 10, 23))}
    assert rows[("himalayas", "2026-10-23")].new_jobs == 2
    assert rows[("himalayas", "2026-10-22")].new_jobs == 0
    assert rows[("linkedin", "2026-10-22")].new_jobs == 1


def test_linkedin_refusals_and_the_two_status_less_buckets(store_conn):
    _event(store_conn, NOON, "neutral", 403)    # a refusal the pace stores as neutral
    _event(store_conn, NOON, "ok", None)        # a successful packaging link check
    _event(store_conn, NOON, "neutral", None)   # a connection error
    store_conn.commit()
    (row,) = daily_requests(store_conn, date(2026, 10, 24), date(2026, 10, 24),
                            sources=["linkedin"])
    assert (row.sent, row.ok, row.refused, row.errors) == (3, 1, 1, 1)
    assert row.codes == (("403", 1), (OK_WITHOUT_STATUS, 1), (NO_STATUS, 1))
    assert row.codes_text == "403:1 nocode:1 none:1"


def test_the_code_buckets_order_numerically_and_put_no_status_last(store_conn):
    for status in (503, 200, 429, None, 503):
        record_request(store_conn, "freehire", NOON, status)
    store_conn.commit()
    (row,) = daily_requests(store_conn, date(2026, 10, 24), date(2026, 10, 24),
                            sources=["freehire"])
    assert row.codes == (("200", 1), ("429", 1), ("503", 2), (NO_STATUS, 1))
    assert row.codes_text == "200:1 429:1 503:2 none:1"
