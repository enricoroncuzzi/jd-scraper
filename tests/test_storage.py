from unittest.mock import MagicMock, patch
from src.models import ScoredOffer
from src.storage import init_db, save_run, save_offers


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
    assert mock_cur.execute.call_count == 2
    sqls = [call[0][0] for call in mock_cur.execute.call_args_list]
    assert any("CREATE TABLE IF NOT EXISTS runs" in s for s in sqls)
    assert any("CREATE TABLE IF NOT EXISTS offers" in s for s in sqls)
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
                    work_mode="remote", score=9, comment="great", summary="LLM role"),
        ScoredOffer(id=1, title="ML Engineer", company="Corp", location="Berlin",
                    link="https://li.com/1", description="another description",
                    work_mode="hybrid", score=7, comment="ok", summary="ML role"),
    ]
    mock_conn, mock_cur = _mock_conn_cur()
    with patch("src.storage.psycopg2.connect", return_value=mock_conn):
        save_offers("postgresql://test", offers, run_id=42, tier=1)
    assert mock_cur.execute.call_count == 2
    mock_conn.commit.assert_called_once()
    mock_conn.close.assert_called_once()


def test_save_offers_does_nothing_on_empty_list():
    with patch("src.storage.psycopg2.connect") as mock_connect:
        save_offers("postgresql://test", [], run_id=42, tier=1)
    mock_connect.assert_not_called()
