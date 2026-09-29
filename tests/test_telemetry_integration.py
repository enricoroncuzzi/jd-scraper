import psycopg2
from tests.integration_db import requires_test_db, test_db_url
from src.storage import init_db


def _columns(cur, table):
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,))
    return {row[0] for row in cur.fetchall()}


@requires_test_db
def test_init_db_evolves_a_production_shaped_database_and_is_rerunnable():
    url = test_db_url()
    init_db(url)
    init_db(url)  # must be idempotent against its own output
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            assert {"run_uuid", "daily_run_id", "status", "telemetry_ok",
                    "verification_rejected"} <= _columns(cur, "runs")
            assert {"stop_reason", "run_uuid"} <= _columns(cur, "run_queries")
            assert {"response_model", "prompt_version", "outcome"} <= _columns(cur, "llm_calls")
            cur.execute("SELECT COUNT(*) FROM runs")
            assert cur.fetchone()[0] > 0  # the branch carries production history; it survived
    finally:
        conn.close()


import json
import os
import uuid
import src.telemetry as telemetry


def _cleanup(url, run_uuid):
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM run_queries WHERE run_uuid = %s", (run_uuid,))
            cur.execute("DELETE FROM llm_calls WHERE run_uuid = %s", (run_uuid,))
            cur.execute("DELETE FROM runs WHERE run_uuid = %s", (run_uuid,))
        conn.commit()
    finally:
        conn.close()


@requires_test_db
def test_full_session_lands_once_even_when_its_buffer_is_replayed(tmp_path, monkeypatch):
    url = test_db_url()
    monkeypatch.setenv("JDS_TELEMETRY_DIR", str(tmp_path))
    telemetry._reset_for_tests()
    session = telemetry.start_session(tier=9, daily_run_id="it-" + uuid.uuid4().hex,
                                      attempt=1, db_url=url)
    try:
        assert session.run_id is not None
        telemetry.set_fields(offers_fetched=5, verification_rejected=2)
        session._append("query", {"id": str(uuid.uuid4()), "run_uuid": session.run_uuid,
                                  "role": "r", "location": "l", "work_mode": None,
                                  "pages_walked": 3, "page_cap": 8, "cards_seen": 25,
                                  "offers_kept": 20, "stop_reason": "empty_page",
                                  "recorded_at": telemetry._utcnow()})
        replay = tmp_path / "replay.jsonl"
        with open(session.buffer_path) as f:
            replay.write_text(f.read())
        telemetry.end_session(status="ok")
        telemetry.flush_buffer_file(str(replay), url)  # the same records, again
        conn = psycopg2.connect(url)
        with conn.cursor() as cur:
            cur.execute("SELECT status, offers_fetched, verification_rejected, telemetry_ok "
                        "FROM runs WHERE run_uuid = %s", (session.run_uuid,))
            assert cur.fetchall() == [("ok", 5, 2, True)]
            cur.execute("SELECT COUNT(*) FROM run_queries WHERE run_uuid = %s", (session.run_uuid,))
            assert cur.fetchone()[0] == 1
        conn.close()
    finally:
        _cleanup(url, session.run_uuid)


@requires_test_db
def test_a_crashed_tier_stays_running_after_the_next_drain(tmp_path, monkeypatch):
    url = test_db_url()
    monkeypatch.setenv("JDS_TELEMETRY_DIR", str(tmp_path))
    telemetry._reset_for_tests()
    crashed = telemetry.start_session(tier=9, daily_run_id="it-" + uuid.uuid4().hex,
                                      attempt=1, db_url=url)
    telemetry._reset_for_tests()  # simulate a hard kill: no end_session
    try:
        telemetry.drain_buffers(str(tmp_path), url)
        conn = psycopg2.connect(url)
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM runs WHERE run_uuid = %s", (crashed.run_uuid,))
            assert cur.fetchone()[0] == "running"
        conn.close()
    finally:
        _cleanup(url, crashed.run_uuid)


@requires_test_db
def test_draining_a_hand_written_run_open_twice_inserts_one_running_row(tmp_path, monkeypatch):
    url = test_db_url()
    monkeypatch.setenv("JDS_TELEMETRY_DIR", str(tmp_path))
    telemetry._reset_for_tests()
    init_db(url)
    run_uuid = str(uuid.uuid4())
    stamp = telemetry._utcnow()
    path = os.path.join(telemetry.buffer_dir(), f"{run_uuid}.jsonl")
    os.makedirs(telemetry.buffer_dir(), exist_ok=True)
    with open(path, "w") as f:
        f.write(json.dumps({
            "kind": "run_open",
            "data": {
                "run_uuid": run_uuid,
                "run_at": stamp,
                "started_at": stamp,
                "tier": 9,
                "status": "running",
                "daily_run_id": "it-" + uuid.uuid4().hex,
                "attempt": 1,
            },
        }) + "\n")
    try:
        telemetry.drain_buffers(telemetry.buffer_dir(), url)
        telemetry.drain_buffers(telemetry.buffer_dir(), url)
        conn = psycopg2.connect(url)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM runs WHERE run_uuid = %s", (run_uuid,))
                assert cur.fetchall() == [("running",)]
        finally:
            conn.close()
    finally:
        _cleanup(url, run_uuid)
