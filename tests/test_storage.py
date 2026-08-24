from unittest.mock import MagicMock, patch, call
from src.models import ScoredOffer
from src.storage import (
    init_db, save_run, save_offers,
    save_application_channel, is_application_packaged,
    save_application, count_applications_packaged_today,
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
    assert mock_cur.execute.call_count == 5
    sqls = [call[0][0] for call in mock_cur.execute.call_args_list]
    assert any("CREATE TABLE IF NOT EXISTS runs" in s for s in sqls)
    assert any("CREATE TABLE IF NOT EXISTS offers" in s for s in sqls)
    assert any("ALTER TABLE offers ADD COLUMN IF NOT EXISTS description_status" in s for s in sqls)
    assert any("ALTER TABLE offers ADD COLUMN IF NOT EXISTS application_channel" in s for s in sqls)
    assert any("CREATE TABLE IF NOT EXISTS applications" in s for s in sqls)
    mock_conn.commit.assert_called_once()
    mock_conn.close.assert_called_once()


def test_save_run_inserts_row_and_returns_id():
    mock_conn, mock_cur = _mock_conn_cur()
    mock_cur.fetchone.return_value = (42,)
    with patch("src.storage.psycopg2.connect", return_value=mock_conn):
        run_id = save_run(
            "postgresql://test",
            tier=1, offers_fetched=97, offers_new=97,
            prompt_tokens=83962, completion_tokens=24454, total_tokens=108416,
        )
    assert run_id == 42
    mock_cur.execute.assert_called_once()
    sql, params = mock_cur.execute.call_args[0]
    assert "INSERT INTO runs" in sql
    assert params == (1, 97, 97, 83962, 24454, 108416)
    mock_conn.commit.assert_called_once()
    mock_conn.close.assert_called_once()


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


def test_save_offers_does_nothing_on_empty_list():
    with patch("src.storage.psycopg2.connect") as mock_connect:
        save_offers("postgresql://test", [], run_id=42, tier=1)
    mock_connect.assert_not_called()


def test_init_db_skips_when_db_url_is_none():
    with patch("src.storage.psycopg2.connect") as mock_connect:
        init_db(None)
    mock_connect.assert_not_called()


def test_save_run_returns_zero_when_db_url_is_none():
    with patch("src.storage.psycopg2.connect") as mock_connect:
        result = save_run(None, tier=1, offers_fetched=10, offers_new=5,
                          prompt_tokens=100, completion_tokens=50, total_tokens=150)
    assert result == 0
    mock_connect.assert_not_called()


def test_init_db_swallows_connect_error(capsys):
    with patch("src.storage.psycopg2.connect", side_effect=Exception("connection refused")):
        init_db("postgresql://bad-url")
    captured = capsys.readouterr()
    assert "[storage]" in captured.out


def test_save_run_swallows_connect_error(capsys):
    with patch("src.storage.psycopg2.connect", side_effect=Exception("connection refused")):
        result = save_run("postgresql://bad-url", tier=1, offers_fetched=10, offers_new=5,
                          prompt_tokens=100, completion_tokens=50, total_tokens=150)
    assert result == 0
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


def test_count_applications_packaged_today_returns_count():
    mock_conn, mock_cur = _mock_conn_cur()
    mock_cur.fetchone.return_value = (3,)
    with patch("src.storage.psycopg2.connect", return_value=mock_conn):
        result = count_applications_packaged_today("postgresql://test")
    assert result == 3


def test_count_applications_packaged_today_zero_when_db_url_is_none():
    with patch("src.storage.psycopg2.connect") as mock_connect:
        result = count_applications_packaged_today(None)
    assert result == 0
    mock_connect.assert_not_called()
