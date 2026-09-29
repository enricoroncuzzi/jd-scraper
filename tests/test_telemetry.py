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
