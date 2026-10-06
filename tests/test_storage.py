from unittest.mock import MagicMock, patch, call
from src.models import ScoredOffer
from src.storage import (
    init_db, save_offers,
    save_application_channel, is_application_packaged,
    save_application,
)


def _mock_conn_cur():
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return mock_conn, mock_cur


def test_init_db_creates_runs_and_offers_tables():
    mock_conn, mock_cur = _mock_conn_cur()
    with patch("src.storage.psycopg2.connect", return_value=mock_conn):
        init_db("postgresql://test")
    assert mock_cur.execute.call_count >= 7
    sqls = [call[0][0] for call in mock_cur.execute.call_args_list]
    assert any("CREATE TABLE IF NOT EXISTS runs" in s for s in sqls)
    assert any("CREATE TABLE IF NOT EXISTS offers" in s for s in sqls)
    assert any("ALTER TABLE offers ADD COLUMN IF NOT EXISTS description_status" in s for s in sqls)
    assert any("ALTER TABLE offers ADD COLUMN IF NOT EXISTS application_channel" in s for s in sqls)
    assert any("CREATE TABLE IF NOT EXISTS applications" in s for s in sqls)
    mock_conn.commit.assert_called_once()
    mock_conn.close.assert_called_once()


def test_init_db_adds_the_verdict_columns():
    mock_conn, mock_cur = _mock_conn_cur()
    with patch("src.storage.psycopg2.connect", return_value=mock_conn):
        init_db("postgresql://test")
    sqls = [call[0][0] for call in mock_cur.execute.call_args_list]
    assert any("ALTER TABLE offers ADD COLUMN IF NOT EXISTS remote_verdict VARCHAR(12)" in s for s in sqls)
    assert any("ALTER TABLE offers ADD COLUMN IF NOT EXISTS remote_reason TEXT" in s for s in sqls)


def test_init_db_evolves_runs_and_adds_telemetry_tables():
    mock_conn, mock_cur = _mock_conn_cur()
    with patch("src.storage.psycopg2.connect", return_value=mock_conn):
        init_db("postgresql://test")
    sql = "\n".join(call[0][0] for call in mock_cur.execute.call_args_list)
    for column in (
        "run_uuid UUID", "daily_run_id TEXT", "attempt INTEGER", "git_commit TEXT",
        "git_dirty BOOLEAN", "status VARCHAR(10)", "started_at TIMESTAMPTZ",
        "finished_at TIMESTAMPTZ", "error TEXT", "offers_deferred INTEGER",
        "offers_packaged INTEGER", "verification_provider TEXT",
        "verification_tokens INTEGER", "verification_batches_failed INTEGER",
        "verification_batches_total INTEGER", "verification_degraded BOOLEAN",
        "verification_confirmed INTEGER", "verification_unconfirmed INTEGER",
        "verification_rejected INTEGER", "search_rate_limits INTEGER",
        "description_rate_limits INTEGER", "rate_limit_deferred INTEGER",
        "rate_limit_dropped INTEGER",
        "telemetry_ok BOOLEAN",
    ):
        assert f"ALTER TABLE runs ADD COLUMN IF NOT EXISTS {column}" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS runs_run_uuid_key ON runs (run_uuid)" in sql
    # An unconditional type change takes ACCESS EXCLUSIVE and can roll back
    # the new runs columns if it waits on a lock. Rate-limited offers are
    # never inserted, so the existing VARCHAR(10) does not need widening.
    assert "ALTER COLUMN description_status" not in sql
    assert "CREATE TABLE IF NOT EXISTS run_queries" in sql
    assert "CREATE TABLE IF NOT EXISTS llm_calls" in sql
    assert "CREATE INDEX IF NOT EXISTS llm_calls_started_at_idx" in sql


def test_save_run_is_gone():
    import src.storage as storage
    assert not hasattr(storage, "save_run")


def test_save_offers_inserts_one_row_per_offer():
    offers = [
        ScoredOffer(id=0, title="AI Engineer", company="Acme", location="Remote",
                    link="https://li.com/0", description="full description text",
                    description_status="ok", work_mode="remote", score=9, comment="great", summary="LLM role"),
        ScoredOffer(id=1, title="ML Engineer", company="Corp", location="Berlin",
                    link="https://li.com/1", description="another description",
                    description_status="partial", work_mode="hybrid", score=7, comment="ok", summary="ML role"),
    ]
    mock_conn, mock_cur = _mock_conn_cur()
    with patch("src.storage.psycopg2.connect", return_value=mock_conn):
        save_offers("postgresql://test", offers, run_id=42, tier=1)
    assert mock_cur.execute.call_count == 2
    mock_conn.commit.assert_called_once()
    mock_conn.close.assert_called_once()


def test_save_offers_includes_description_status_in_insert():
    offers = [
        ScoredOffer(id=0, title="AI Engineer", company="Acme", location="Remote",
                    link="https://li.com/0", description="",
                    description_status="failed", work_mode="remote", score=1, comment="c", summary="s"),
    ]
    mock_conn, mock_cur = _mock_conn_cur()
    with patch("src.storage.psycopg2.connect", return_value=mock_conn):
        save_offers("postgresql://test", offers, run_id=1, tier=1)
    sql, params = mock_cur.execute.call_args[0]
    assert "description_status" in sql
    assert "failed" in params


def test_save_offers_persists_the_verdict():
    offers = [
        ScoredOffer(id=0, title="AI Engineer", company="Acme", location="Remote",
                    link="https://li.com/0", description="full description text",
                    description_status="ok", work_mode="remote", score=9, comment="great", summary="LLM role",
                    remote_verdict="confirmed", remote_reason="job page states fully remote"),
    ]
    mock_conn, mock_cur = _mock_conn_cur()
    with patch("src.storage.psycopg2.connect", return_value=mock_conn):
        save_offers("postgresql://test", offers, run_id=1, tier=1)
    sql, params = mock_cur.execute.call_args[0]
    assert "remote_verdict" in sql
    assert "remote_reason" in sql
    assert "confirmed" in params
    assert "job page states fully remote" in params


def test_save_offers_does_nothing_on_empty_list():
    with patch("src.storage.psycopg2.connect") as mock_connect:
        assert save_offers("postgresql://test", [], run_id=42, tier=1) is True
    mock_connect.assert_not_called()


def test_save_offers_returns_false_when_the_write_fails():
    offers = [
        ScoredOffer(id=0, title="AI Engineer", company="Acme", location="Remote",
                    link="https://li.com/0", description="text",
                    description_status="ok", work_mode="remote", score=9, comment="c", summary="s"),
    ]
    with patch("src.storage.psycopg2.connect", side_effect=OSError("neon down")):
        assert save_offers("postgresql://test", offers, run_id=1, tier=1) is False


def test_save_offers_creates_its_own_run_when_telemetry_has_no_run_id():
    offers = [
        ScoredOffer(id=0, title="AI Engineer", company="Acme", location="Remote",
                    link="https://li.com/0", description="text",
                    description_status="ok", work_mode="remote", score=9, comment="c", summary="s"),
    ]
    run_data = {
        "run_uuid": "ad6e15b9-b4c8-4784-8937-0be274350bf3",
        "run_at": "2026-10-01T08:00:00+00:00",
        "daily_run_id": "day-1",
        "attempt": 1,
        "git_commit": "a" * 40,
        "git_dirty": False,
        "status": "running",
        "started_at": "2026-10-01T08:00:00+00:00",
    }
    mock_conn, mock_cur = _mock_conn_cur()
    mock_cur.fetchone.return_value = (73,)

    with patch("src.storage.psycopg2.connect", return_value=mock_conn):
        assert save_offers(
            "postgresql://test", offers, run_id=None, tier=1, run_data=run_data,
        ) is True

    run_sql, run_params = mock_cur.execute.call_args_list[0].args
    assert "INSERT INTO runs" in run_sql and "RETURNING id" in run_sql
    assert run_data["run_uuid"] in run_params
    offer_sql, offer_params = mock_cur.execute.call_args_list[1].args
    assert "INSERT INTO offers" in offer_sql
    assert offer_params[0] == 73
    mock_conn.commit.assert_called_once()


def test_telemetry_init_db_bounds_the_connection():
    mock_conn, mock_cur = _mock_conn_cur()
    with patch("src.storage.psycopg2.connect", return_value=mock_conn) as connect:
        init_db("postgresql://test", connect_timeout=10)
    assert connect.call_args.kwargs["connect_timeout"] == 10
    assert "options" not in connect.call_args.kwargs
    sqls = [call.args[0] for call in mock_cur.execute.call_args_list]
    assert sqls[:2] == [
        "SET LOCAL statement_timeout = '15000ms'",
        "SET LOCAL lock_timeout = '5000ms'",
    ]


def test_init_db_skips_when_db_url_is_none():
    with patch("src.storage.psycopg2.connect") as mock_connect:
        init_db(None)
    mock_connect.assert_not_called()


def test_init_db_swallows_connect_error(capsys):
    with patch("src.storage.psycopg2.connect", side_effect=Exception("connection refused")):
        init_db("postgresql://bad-url")
    captured = capsys.readouterr()
    assert "[storage]" in captured.out


def test_save_application_channel_updates_offers_by_link():
    mock_conn, mock_cur = _mock_conn_cur()
    with patch("src.storage.psycopg2.connect", return_value=mock_conn):
        save_application_channel("postgresql://test", "https://li.com/1", "external_ats")
    sql, params = mock_cur.execute.call_args[0]
    assert "UPDATE offers" in sql
    assert params == ("external_ats", "https://li.com/1")
    mock_conn.commit.assert_called_once()


def test_save_application_channel_skips_when_db_url_is_none():
    with patch("src.storage.psycopg2.connect") as mock_connect:
        save_application_channel(None, "https://li.com/1", "external_ats")
    mock_connect.assert_not_called()


def test_is_application_packaged_true_when_row_found():
    mock_conn, mock_cur = _mock_conn_cur()
    mock_cur.fetchone.return_value = (1,)
    with patch("src.storage.psycopg2.connect", return_value=mock_conn):
        result = is_application_packaged("postgresql://test", "https://li.com/1")
    assert result is True
    sql = mock_cur.execute.call_args[0][0]
    assert "dry_run" not in sql


def test_is_application_packaged_false_when_no_row():
    mock_conn, mock_cur = _mock_conn_cur()
    mock_cur.fetchone.return_value = None
    with patch("src.storage.psycopg2.connect", return_value=mock_conn):
        result = is_application_packaged("postgresql://test", "https://li.com/1")
    assert result is False


def test_is_application_packaged_false_when_db_url_is_none():
    with patch("src.storage.psycopg2.connect") as mock_connect:
        result = is_application_packaged(None, "https://li.com/1")
    assert result is False
    mock_connect.assert_not_called()


def test_save_application_inserts_with_dedup_key():
    mock_conn, mock_cur = _mock_conn_cur()
    with patch("src.storage.psycopg2.connect", return_value=mock_conn):
        save_application(
            "postgresql://test", "https://li.com/1", "AI Engineer", "Acme",
            "linkedin_easy_apply", dry_run=False,
        )
    sql, params = mock_cur.execute.call_args[0]
    assert "INSERT INTO applications" in sql
    assert "ON CONFLICT" in sql
    assert params[1:] == ("https://li.com/1", "AI Engineer", "Acme", "linkedin_easy_apply", False)
    mock_conn.commit.assert_called_once()


def test_save_application_skips_when_db_url_is_none():
    with patch("src.storage.psycopg2.connect") as mock_connect:
        save_application(None, "https://li.com/1", "t", "c", "email_apply", dry_run=True)
    mock_connect.assert_not_called()


