import psycopg2
from tests.integration_db import requires_test_db, test_db_url
from src.models import ScoredOffer
from src.storage import init_db, save_offers
from src import report_data, telemetry


def _columns(cur, table):
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,))
    return {row[0] for row in cur.fetchall()}


@requires_test_db
def test_bounded_connections_work_through_the_pooled_endpoint(capsys):
    url = test_db_url()
    assert "-pooler." in url

    init_db(url, connect_timeout=10)
    assert "[storage] init_db failed" not in capsys.readouterr().out

    for connect in (telemetry._connect, report_data._connect):
        conn = connect(url)
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW statement_timeout")
                assert cur.fetchone()[0] == "15s"
                cur.execute("SHOW lock_timeout")
                assert cur.fetchone()[0] == "5s"
        finally:
            conn.rollback()
            conn.close()


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


def _cleanup(url, run_uuid):
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM run_queries WHERE run_uuid = %s", (run_uuid,))
            cur.execute("DELETE FROM llm_calls WHERE run_uuid = %s", (run_uuid,))
            cur.execute("DELETE FROM offers WHERE run_id IN "
                        "(SELECT id FROM runs WHERE run_uuid = %s)", (run_uuid,))
            cur.execute("DELETE FROM runs WHERE run_uuid = %s", (run_uuid,))
        conn.commit()
    finally:
        conn.close()


@requires_test_db
def test_offer_storage_survives_a_telemetry_connection_failure(tmp_path, monkeypatch):
    from unittest.mock import patch

    url = test_db_url()
    monkeypatch.setenv("JDS_TELEMETRY_DIR", str(tmp_path))
    telemetry._reset_for_tests()
    with patch("src.telemetry.flush_records",
               side_effect=psycopg2.OperationalError("telemetry connection failed")):
        session = telemetry.start_session(
            tier=9,
            daily_run_id="it-" + uuid.uuid4().hex,
            attempt=1,
            db_url=url,
        )
    assert session.run_id is None
    offer = ScoredOffer(
        id=0,
        title="AI Engineer",
        company="Acme",
        location="Remote",
        link=f"https://example.test/{uuid.uuid4()}",
        description="description",
        description_status="ok",
        work_mode="remote",
        score=9,
        comment="strong match",
        summary="summary",
    )

    try:
        assert save_offers(
            url,
            [offer],
            run_id=None,
            tier=9,
            run_data=session.open_record,
        )
        telemetry.end_session(status="ok")

        conn = psycopg2.connect(url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT r.status, COUNT(o.id) FROM runs r "
                    "LEFT JOIN offers o ON o.run_id = r.id "
                    "WHERE r.run_uuid = %s GROUP BY r.status",
                    (session.run_uuid,),
                )
                assert cur.fetchall() == [("ok", 1)]
        finally:
            conn.close()
        assert not os.path.exists(session.buffer_path)
    finally:
        telemetry._reset_for_tests()
        _cleanup(url, session.run_uuid)


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


from datetime import date, timedelta
@requires_test_db
def test_load_day_and_metrics_read_back_a_recorded_run(tmp_path, monkeypatch):
    url = test_db_url()
    monkeypatch.setenv("JDS_TELEMETRY_DIR", str(tmp_path))
    telemetry._reset_for_tests()
    daily = "it-" + uuid.uuid4().hex
    session = telemetry.start_session(tier=1, daily_run_id=daily, attempt=1, db_url=url)
    try:
        telemetry.set_fields(offers_fetched=40, verification_tokens=1234, verification_rejected=3)
        telemetry.add_query(role="AI Engineer", location="Italy", work_mode="remote",
                            pages_walked=30, page_cap=30, cards_seen=300, offers_kept=250,
                            stop_reason="cap_hit")
        with telemetry.llm_call(stage="verification", provider="groq",
                                request_model="openai/gpt-oss-20b", batch_size=8, attempt=1,
                                prompt_version="a" * 12) as call:
            call.set_usage(response_model="openai/gpt-oss-20b", input_tokens=900, output_tokens=100)
        telemetry.end_session(status="ok")
        settings = report_data.load_tier_settings([f"config/config_tier{n}.json" for n in (1, 2, 3, 4)])
        report = report_data.load_day(url, settings=settings, daily_run_id=daily)
        assert report.tiers[1].status == "ok" and report.tiers[1].offers_fetched == 40
        assert [q.stop_reason for q in report.queries] == ["cap_hit"]
        [stage] = report.stages
        assert stage.stage == "verification" and stage.tokens == 1000
        groq20 = [l for l in report.limits if l.model == "openai/gpt-oss-20b"][0]
        assert groq20.used >= 1000 and groq20.used_through_tier[1] >= 1000
        metrics = report_data.load_daily_metrics(url, since=date.today() - timedelta(days=2),
                                                 settings=settings)
        assert any(m.cap_hits and m.cap_hits >= 1 for m in metrics)
        models, verdicts = report_data.load_llm_view(url, since=date.today() - timedelta(days=1),
                                                     stage="verification")
        assert any(m["model"] == "openai/gpt-oss-20b" for m in models)
    finally:
        _cleanup(url, session.run_uuid)
