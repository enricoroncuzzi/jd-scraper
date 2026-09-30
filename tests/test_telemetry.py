import json
import os
from unittest.mock import MagicMock, patch
import pytest
import src.telemetry as telemetry


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("JDS_TELEMETRY_DIR", str(tmp_path / "telemetry"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    telemetry._reset_for_tests()
    yield
    telemetry._reset_for_tests()


def _lines(path):
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def test_prompt_version_is_stable_and_sensitive():
    a = telemetry.prompt_version("system", "human")
    assert a == telemetry.prompt_version("system", "human")
    assert a != telemetry.prompt_version("system", "human!")
    assert len(a) == 12 and all(c in "0123456789abcdef" for c in a)


def test_read_git_info_never_raises(tmp_path):
    assert telemetry.read_git_info(str(tmp_path)) == telemetry.GitInfo("unknown", False)
    info = telemetry.read_git_info(".")
    assert len(info.commit) == 40


def test_session_without_database_is_disabled_and_writes_nothing(tmp_path):
    session = telemetry.start_session(tier=1, daily_run_id="d", attempt=1, db_url=None)
    telemetry.set_fields(offers_fetched=3)
    telemetry.count("search_rate_limits")
    telemetry.end_session()
    assert session.enabled is False
    assert not os.path.exists(telemetry.buffer_dir())


def test_open_flush_success_deletes_buffer_and_looks_up_run_id():
    with patch("src.telemetry.storage.init_db"), \
         patch("src.telemetry.flush_records") as flush, \
         patch("src.telemetry._lookup_run_id", return_value=41):
        session = telemetry.start_session(tier=2, daily_run_id="d1", attempt=1, db_url="postgresql://x")
    assert session.run_id == 41
    assert not os.path.exists(session.buffer_path)
    (records, url), _ = flush.call_args
    assert url == "postgresql://x"
    assert records[0]["kind"] == "run_open"
    data = records[0]["data"]
    assert data["status"] == "running" and data["tier"] == 2 and data["attempt"] == 1
    assert data["run_uuid"] == session.run_uuid and data["daily_run_id"] == "d1"


def test_neon_down_keeps_every_record_in_the_buffer_and_marks_incomplete():
    with patch("src.telemetry.storage.init_db"), \
         patch("src.telemetry.flush_records", side_effect=OSError("neon down")):
        session = telemetry.start_session(tier=1, daily_run_id="d", attempt=1, db_url="postgresql://x")
        telemetry.set_fields(offers_fetched=7)
        telemetry.count("description_rate_limits", 2)
        telemetry.end_session(status="ok")
    kinds = [l["kind"] for l in _lines(session.buffer_path)]
    assert kinds == ["run_open", "run_close"]
    close = _lines(session.buffer_path)[1]["data"]
    assert close["offers_fetched"] == 7 and close["description_rate_limits"] == 2
    assert session.telemetry_ok is False
    assert session.run_id is None


def test_end_session_derives_degraded_from_verification():
    with patch("src.telemetry.storage.init_db"), \
         patch("src.telemetry.flush_records") as flush, \
         patch("src.telemetry._lookup_run_id", return_value=1):
        telemetry.start_session(tier=1, daily_run_id="d", attempt=1, db_url="postgresql://x")
        telemetry.set_fields(verification_degraded=True)
        telemetry.end_session()
    close = flush.call_args_list[-1][0][0][-1]
    assert close["kind"] == "run_close" and close["data"]["status"] == "degraded"


def test_failed_status_carries_a_truncated_error():
    with patch("src.telemetry.storage.init_db"), \
         patch("src.telemetry.flush_records") as flush, \
         patch("src.telemetry._lookup_run_id", return_value=1):
        telemetry.start_session(tier=1, daily_run_id="d", attempt=2, db_url="postgresql://x")
        telemetry.end_session(status="failed", error="RuntimeError: " + "x" * 2000)
    close = flush.call_args_list[-1][0][0][-1]["data"]
    assert close["status"] == "failed" and len(close["error"]) <= 500


def test_drain_skips_a_torn_last_line_and_flushes_the_valid_ones(tmp_path):
    d = telemetry.buffer_dir()
    os.makedirs(d)
    path = os.path.join(d, "old.jsonl")
    with open(path, "w") as f:
        f.write(json.dumps({"kind": "run_open", "data": {"run_uuid": "u"}}) + "\n")
        f.write('{"kind": "query", "data": {"id": "q1"')  # killed mid-write
    with patch("src.telemetry.flush_records") as flush:
        telemetry.drain_buffers(d, "postgresql://x")
    (records, _), _ = flush.call_args
    assert [r["kind"] for r in records] == ["run_open"]
    assert not os.path.exists(path)


def test_drain_excludes_the_current_buffer(tmp_path):
    d = telemetry.buffer_dir()
    os.makedirs(d)
    mine = os.path.join(d, "mine.jsonl")
    with open(mine, "w") as f:
        f.write(json.dumps({"kind": "run_open", "data": {}}) + "\n")
    with patch("src.telemetry.flush_records") as flush:
        telemetry.drain_buffers(d, "postgresql://x", exclude=mine)
    flush.assert_not_called()
    assert os.path.exists(mine)


def test_no_telemetry_failure_ever_escapes():
    with patch("src.telemetry.storage.init_db", side_effect=RuntimeError("boom")), \
         patch("src.telemetry.flush_records", side_effect=RuntimeError("boom")), \
         patch("src.telemetry.TierSession._append", side_effect=OSError("disk full")):
        telemetry.start_session(tier=1, daily_run_id="d", attempt=1, db_url="postgresql://x")
        telemetry.set_fields(offers_fetched=1)
        telemetry.count("search_rate_limits")
        telemetry.end_session(status="ok")
    # reaching this line is the assertion


def test_ensure_run_id_retries_the_open_flush_later():
    with patch("src.telemetry.storage.init_db"), \
         patch("src.telemetry.flush_records", side_effect=[OSError("blip"), None]), \
         patch("src.telemetry._lookup_run_id", return_value=9):
        session = telemetry.start_session(tier=1, daily_run_id="d", attempt=1, db_url="postgresql://x")
        assert session.run_id is None
        assert session.ensure_run_id() == 9


def test_buffer_append_failure_still_inserts_the_run_row():
    with patch("src.telemetry.storage.init_db"), \
         patch("src.telemetry.flush_records") as flush, \
         patch("src.telemetry._lookup_run_id", return_value=7), \
         patch("src.telemetry.TierSession._append", side_effect=OSError("disk full")):
        session = telemetry.start_session(tier=1, daily_run_id="d", attempt=1, db_url="postgresql://x")
    assert session.run_id == 7
    assert flush.call_args[0][0][0]["kind"] == "run_open"


def test_failed_close_flush_persists_telemetry_ok_false():
    def flaky(records, url):
        if any(r.get("kind") == "run_close" for r in records):
            raise OSError("neon down")
    with patch("src.telemetry.storage.init_db"), \
         patch("src.telemetry.flush_records", side_effect=flaky), \
         patch("src.telemetry._lookup_run_id", return_value=1):
        session = telemetry.start_session(tier=1, daily_run_id="d", attempt=1, db_url="postgresql://x")
        telemetry.end_session(status="ok")
    close = _lines(session.buffer_path)[-1]["data"]
    assert close["telemetry_ok"] is False
    assert session.telemetry_ok is False


def test_drain_stops_after_the_first_connectivity_failure(tmp_path):
    import psycopg2
    d = tmp_path / "buffers"
    d.mkdir()
    for name in ("a.jsonl", "b.jsonl", "c.jsonl"):
        (d / name).write_text(json.dumps({"kind": "run_open", "data": {"run_uuid": name}}) + "\n")
    calls = []

    def boom(records, url):
        calls.append(records[0]["data"]["run_uuid"])
        raise psycopg2.OperationalError("timeout")

    with patch("src.telemetry.flush_records", side_effect=boom):
        telemetry.drain_buffers(str(d), "postgresql://x")
    assert calls == ["a.jsonl"]
    assert sorted(os.listdir(d)) == ["a.jsonl", "b.jsonl", "c.jsonl"]


def test_observability_connects_are_bounded():
    conn = MagicMock()
    with patch("src.telemetry.psycopg2.connect", return_value=conn) as connect:
        telemetry.flush_records([], "postgresql://x")
    assert connect.call_args.kwargs["connect_timeout"] == 10
    assert "statement_timeout" in connect.call_args.kwargs["options"]
    assert "lock_timeout" in connect.call_args.kwargs["options"]


def test_end_session_degrades_when_offers_were_not_saved():
    with patch("src.telemetry.storage.init_db"), \
         patch("src.telemetry.flush_records") as flush, \
         patch("src.telemetry._lookup_run_id", return_value=1):
        session = telemetry.start_session(tier=1, daily_run_id="d", attempt=1, db_url="postgresql://x")
        session.storage_failed = True
        telemetry.end_session()
    close = flush.call_args_list[-1][0][0][-1]["data"]
    assert close["status"] == "degraded"
    assert "not saved" in close["error"]


def test_ensure_run_id_retries_lookup_after_a_successful_flush_deleted_the_buffer():
    with patch("src.telemetry.storage.init_db"), \
         patch("src.telemetry.flush_records"), \
         patch("src.telemetry._lookup_run_id", side_effect=[OSError("lookup blip"), 9]):
        session = telemetry.start_session(tier=1, daily_run_id="d", attempt=1, db_url="postgresql://x")
        assert session.run_id is None
        # The open-time lookup may already have marked the session incomplete.
        # Retrying against a buffer the successful flush already deleted must not.
        session.telemetry_ok = True
        assert session.ensure_run_id() == 9
        assert session.telemetry_ok is True


import openai
import groq
import httpx
from pydantic import BaseModel, ValidationError


def _rate_limit(cls, body):
    request = httpx.Request("POST", "https://x")
    return cls(message="429", response=httpx.Response(429, request=request), body=body)


def _enabled_session():
    patches = [patch("src.telemetry.storage.init_db"),
               patch("src.telemetry.flush_records", side_effect=OSError("keep in buffer"))]
    for p in patches:
        p.start()
    session = telemetry.start_session(tier=1, daily_run_id="d", attempt=1, db_url="postgresql://x")
    # The open flush failed on purpose (to keep records readable in the buffer),
    # which marked the session incomplete. Reset that, so each test's own
    # telemetry_ok assertion measures only what that test did.
    session.telemetry_ok = True
    telemetry._warned_sites.clear()
    return session, patches


def _llm_records(session):
    return [l["data"] for l in _lines(session.buffer_path) if l["kind"] == "llm_call"]


def test_classify_llm_error_covers_every_outcome():
    class M(BaseModel):
        n: int
    try:
        M.model_validate({"n": "x"})
    except ValidationError as e:
        validation_error = e
    assert telemetry.classify_llm_error(_rate_limit(openai.RateLimitError, {})) == "rate_limited"
    assert telemetry.classify_llm_error(_rate_limit(groq.RateLimitError, {})) == "rate_limited"
    assert telemetry.classify_llm_error(
        _rate_limit(groq.RateLimitError, {}), is_quota_exhausted=lambda e: True) == "quota_exhausted"
    assert telemetry.classify_llm_error(
        openai.APITimeoutError(request=httpx.Request("POST", "https://x"))) == "timeout"
    assert telemetry.classify_llm_error(validation_error) == "invalid_output"
    assert telemetry.classify_llm_error(telemetry.InvalidLLMOutput()) == "invalid_output"
    assert telemetry.classify_llm_error(KeyError("x")) == "error"
    assert telemetry.classify_llm_error(
        KeyError("x"), is_quota_exhausted=lambda e: 1 / 0) == "error"  # a broken checker never escapes


def test_llm_call_success_records_usage_and_latency():
    session, patches = _enabled_session()
    try:
        with telemetry.llm_call(stage="scoring", provider="openrouter", request_model="req",
                                batch_size=5, attempt=1, prompt_version="abc123abc123") as call:
            call.set_usage(response_model="served", input_tokens=100, output_tokens=20)
        [record] = _llm_records(session)
        assert record["outcome"] == "ok" and record["response_model"] == "served"
        assert record["input_tokens"] == 100 and record["output_tokens"] == 20
        assert record["run_uuid"] == session.run_uuid and record["latency_ms"] >= 0
        assert record["stage"] == "scoring" and record["attempt"] == 1
    finally:
        for p in patches:
            p.stop()


def test_llm_call_reraises_the_callers_exception_unchanged_and_records_it():
    session, patches = _enabled_session()
    try:
        boom = _rate_limit(openai.RateLimitError, {})
        with pytest.raises(openai.RateLimitError) as raised:
            with telemetry.llm_call(stage="verification", provider="openrouter", request_model="m",
                                    batch_size=8, attempt=3, prompt_version="p" * 12):
                raise boom
        assert raised.value is boom
        [record] = _llm_records(session)
        assert record["outcome"] == "rate_limited" and record["attempt"] == 3
        assert record["error"].startswith("RateLimitError")
    finally:
        for p in patches:
            p.stop()


def test_tokens_spent_on_an_invalid_answer_are_still_recorded():
    session, patches = _enabled_session()
    try:
        with pytest.raises(telemetry.InvalidLLMOutput):
            with telemetry.llm_call(stage="scoring", provider="openrouter", request_model="m",
                                    batch_size=5, attempt=1, prompt_version="p" * 12) as call:
                call.set_usage(response_model="m", input_tokens=900, output_tokens=3)
                raise telemetry.InvalidLLMOutput()
        [record] = _llm_records(session)
        assert record["outcome"] == "invalid_output" and record["input_tokens"] == 900
    finally:
        for p in patches:
            p.stop()


def test_llm_call_with_no_session_and_no_database_is_a_silent_noop():
    with telemetry.llm_call(stage="tailoring", provider="groq", request_model="m",
                            batch_size=1, attempt=1, prompt_version="p" * 12) as call:
        call.set_usage(response_model="m", input_tokens=1, output_tokens=1)


def test_llm_call_with_no_session_writes_directly_when_a_database_is_configured(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://manual")
    with patch("src.telemetry.flush_records") as flush:
        with telemetry.llm_call(stage="tailoring", provider="groq", request_model="m",
                                batch_size=1, attempt=1, prompt_version="p" * 12):
            pass
    (records, url), _ = flush.call_args
    assert url == "postgresql://manual"
    assert records[0]["kind"] == "llm_call" and records[0]["data"]["run_uuid"] is None


def test_a_recording_failure_never_masks_or_replaces_the_callers_result():
    session, patches = _enabled_session()
    try:
        with patch("src.telemetry.TierSession._append", side_effect=OSError("disk full")):
            with telemetry.llm_call(stage="scoring", provider="openrouter", request_model="m",
                                    batch_size=1, attempt=1, prompt_version="p" * 12):
                value = 42
        assert value == 42
        assert session.telemetry_ok is False
    finally:
        for p in patches:
            p.stop()


def test_add_query_rejects_an_unknown_stop_reason_without_raising():
    session, patches = _enabled_session()
    try:
        telemetry.add_query(role="r", location="l", work_mode=None, pages_walked=1, page_cap=8,
                            cards_seen=10, offers_kept=9, stop_reason="cap_hit")
        telemetry.add_query(role="r", location="l", work_mode=None, pages_walked=1, page_cap=8,
                            cards_seen=10, offers_kept=9, stop_reason="made_up")
        queries = [l["data"] for l in _lines(session.buffer_path) if l["kind"] == "query"]
        assert [q["stop_reason"] for q in queries] == ["cap_hit"]
        assert session.telemetry_ok is False
    finally:
        for p in patches:
            p.stop()


def test_add_query_records_cross_query_duplicates_and_defaults_it_to_zero():
    session, patches = _enabled_session()
    try:
        telemetry.add_query(role="r", location="l", work_mode=None, pages_walked=1, page_cap=8,
                            cards_seen=10, offers_kept=9, stop_reason="cap_hit",
                            cross_query_duplicates=4)
        telemetry.add_query(role="r", location="l", work_mode=None, pages_walked=1, page_cap=8,
                            cards_seen=10, offers_kept=9, stop_reason="cap_hit")
        queries = [l["data"] for l in _lines(session.buffer_path) if l["kind"] == "query"]
        assert [q["cross_query_duplicates"] for q in queries] == [4, 0]
        assert "cross_query_duplicates" in telemetry._QUERY_COLUMNS
    finally:
        for p in patches:
            p.stop()


def test_redact_masks_telegram_bot_token_in_request_errors():
    from src import telemetry
    msg = "Max retries exceeded with url: /bot123456:AAE-x_y/sendMessage (Caused by ...)"
    out = telemetry._redact(msg)
    assert "AAE-x_y" not in out and "/sendMessage" in out
