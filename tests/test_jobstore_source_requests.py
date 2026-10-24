from datetime import datetime, timezone

from src.jobstore.source_requests import SourceRequestLog, outcome_for_status, record_request

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


def test_each_attempt_stores_the_outcome_for_its_status(store_conn):
    record_request(store_conn, "freehire", NOW, 200)
    record_request(store_conn, "freehire", NOW, 429)
    record_request(store_conn, "freehire", NOW, None)
    store_conn.commit()
    with store_conn.cursor() as cur:
        cur.execute("SELECT source, outcome, status FROM source_requests ORDER BY id")
        assert cur.fetchall() == [("freehire", "ok", 200), ("freehire", "refused", 429),
                                  ("freehire", "error", None)]


def test_the_outcome_mapping_covers_every_answer():
    assert outcome_for_status(200) == "ok"
    assert outcome_for_status(204) == "ok"
    assert outcome_for_status(403) == "refused"
    assert outcome_for_status(429) == "refused"
    assert outcome_for_status(404) == "error"
    assert outcome_for_status(500) == "error"
    assert outcome_for_status(None) == "error"


def test_the_recorder_writes_through_its_own_connection(store_conn, store_conn_factory):
    log = SourceRequestLog("himalayas", connect_fn=lambda: store_conn_factory(store_conn),
                           now=lambda: NOW)
    log(200)
    log(503)
    with store_conn.cursor() as cur:
        cur.execute("SELECT source, outcome, status, at FROM source_requests ORDER BY id")
        assert cur.fetchall() == [("himalayas", "ok", 200, NOW),
                                  ("himalayas", "error", 503, NOW)]


def test_a_recording_failure_is_swallowed_and_logged(capsys):
    def broken():
        raise RuntimeError("store down")

    SourceRequestLog("freehire", connect_fn=broken)(200)
    assert "could not record" in capsys.readouterr().out
