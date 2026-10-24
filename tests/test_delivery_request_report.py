from datetime import datetime, timezone

from src.delivery.request_report import request_lines
from src.jobstore.sightings import CardInput, record_sighting
from src.linkedin.schedule import ScheduleConfig

NOW = datetime(2026, 10, 14, 6, 0, tzinfo=timezone.utc)  # 08:00 Rome, delivery time


def _event(conn, at, outcome, status=None):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO pace_events (at, service, kind, outcome, status) "
                    "VALUES (%s, 'collect-linkedin', 'search', %s, %s)", (at, outcome, status))
    conn.commit()


def test_one_line_per_source_for_yesterday_with_the_change_against_the_day_before(store_conn):
    yesterday = datetime(2026, 10, 13, 10, 0, tzinfo=timezone.utc)
    before = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)
    for _ in range(8):
        _event(store_conn, before, "ok", 200)
    for _ in range(10):
        _event(store_conn, yesterday, "ok", 200)
    for _ in range(2):
        _event(store_conn, yesterday, "refused", 429)
    _event(store_conn, yesterday, "neutral")
    for slug in ("a", "b", "c", "d"):
        record_sighting(store_conn, CardInput(source="linkedin", source_job_id=slug,
                                              source_url=f"https://l/{slug}", title="t", company="c"),
                        yesterday)
    store_conn.commit()
    lines = request_lines(store_conn, NOW, schedule=ScheduleConfig())
    assert lines[0] == "Requests per source yesterday (Rome)"
    assert lines[1] == ("linkedin 2026-10-13: 13 sent (10 ok, 2 refused, 1 errors), 4 new jobs, "
                        "codes 200:10 429:2 none:1; 8 the day before (+5)")
    assert lines[2] == ("freehire 2026-10-13: 0 sent (0 ok, 0 refused, 0 errors), 0 new jobs, "
                        "codes -; 0 the day before (+0)")
    assert [line.split()[0] for line in lines[1:]] == ["linkedin", "freehire", "himalayas"]


def test_the_line_counts_a_negative_change(store_conn):
    _event(store_conn, datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc), "ok", 200)
    _event(store_conn, datetime(2026, 10, 12, 11, 0, tzinfo=timezone.utc), "ok", 200)
    _event(store_conn, datetime(2026, 10, 13, 10, 0, tzinfo=timezone.utc), "ok", 200)
    store_conn.commit()
    line = request_lines(store_conn, NOW, schedule=ScheduleConfig())[1]
    assert line.endswith("2 the day before (-1)")
