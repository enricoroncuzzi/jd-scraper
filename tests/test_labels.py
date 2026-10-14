from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

import label as label_cli
from src import labels
from src.storage import init_db

LINK = "https://it.linkedin.com/jobs/view/ai-engineer-at-acme-3912345678"


def _conn(fetch=(True,), rowcount=0):
    conn, cur = MagicMock(), MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = fetch
    cur.rowcount = rowcount
    return conn, cur


def _note(tmp_path, link=LINK):
    d = tmp_path / "2026-10-11" / "tier1" / "scraped"
    d.mkdir(parents=True)
    (d / "acme_ai_engineer_7.md").write_text(
        f"---\ndate: 2026-10-11\ntier: 1\nscore: 9\ncompany: Acme\nlink: {link}\n---\n\n"
        "# AI Engineer - Acme\n\n## Job Description\n\nbody\n"
    )
    return "2026-10-11/tier1/scraped/acme_ai_engineer_7.md"


def test_init_db_creates_offer_labels_idempotently():
    conn, cur = _conn()
    with patch("src.storage.psycopg2.connect", return_value=conn):
        init_db("postgresql://t")
    sql = "\n".join(c[0][0] for c in cur.execute.call_args_list)
    assert "CREATE TABLE IF NOT EXISTS offer_labels" in sql
    assert "UNIQUE (posting_key, label)" in sql
    assert "CHECK (label IN ('golden', 'not_for_me'))" in sql
    assert "job_id      UUID" in sql


def test_posting_key_uses_linkedin_job_id():
    assert labels.posting_key(LINK) == "linkedin:3912345678"
    assert labels.posting_key("https://www.linkedin.com/jobs/view/3912345678/?x=1") == "linkedin:3912345678"
    assert labels.posting_key("https://Example.com/jobs/a/?q=1#f") == "example.com/jobs/a"


def test_set_label_upsert_is_idempotent_and_reports_duplicates():
    conn, cur = _conn(fetch=(False,))
    with patch("src.labels.psycopg2.connect", return_value=conn):
        res = labels.set_label("postgresql://t", LINK, "golden", "n.md")
    assert res == {"created": False, "replaced": None}
    sqls = [c[0][0] for c in cur.execute.call_args_list]
    assert any("ON CONFLICT (posting_key, label)" in s for s in sqls)
    conn.commit.assert_called_once()
    conn.close.assert_called_once()


def test_set_label_removes_the_opposite_label():
    conn, cur = _conn(fetch=(True,), rowcount=1)
    with patch("src.labels.psycopg2.connect", return_value=conn):
        res = labels.set_label("postgresql://t", LINK, "not_for_me")
    assert res == {"created": True, "replaced": "golden"}
    delete = next(c for c in cur.execute.call_args_list if "DELETE FROM offer_labels" in c[0][0])
    assert "label <> %s" in delete[0][0]
    assert delete[0][1] == ("linkedin:3912345678", "not_for_me")


def test_set_label_rejects_unknown_label_and_missing_db():
    with pytest.raises(ValueError):
        labels.set_label("postgresql://t", LINK, "great")
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        labels.set_label(None, LINK, "golden")


def test_parse_label_uri():
    assert labels.parse_label_uri("jdlabel:golden/2026-10-11/tier1/scraped/a_1.md") == (
        "golden", "2026-10-11/tier1/scraped/a_1.md")
    with pytest.raises(ValueError, match="unknown label"):
        labels.parse_label_uri("jdlabel:bad/2026/x.md")


def test_resolve_note_and_read_note(tmp_path):
    rel = _note(tmp_path)
    info = labels.read_note(labels.resolve_note(rel, str(tmp_path)))
    assert info["link"] == LINK and info["title"] == "AI Engineer" and info["score"] == "9"
    with pytest.raises(ValueError, match="outside"):
        labels.resolve_note("../../etc/passwd", str(tmp_path))
    with pytest.raises(ValueError, match="not found"):
        labels.read_note(str(tmp_path / "missing.md"))


def test_cli_uri_saves_and_notifies(tmp_path, monkeypatch):
    rel = _note(tmp_path)
    sent = []
    monkeypatch.setattr(label_cli, "notify", lambda t, m: sent.append((t, m)))
    calls = []
    monkeypatch.setattr(labels, "set_label", lambda *a: calls.append(a) or {"created": True, "replaced": None})
    msg = label_cli.handle_uri(f"jdlabel:golden/{rel}", str(tmp_path), "postgresql://t")
    assert calls == [("postgresql://t", LINK, "golden", "acme_ai_engineer_7.md")]
    assert sent == [("Offer labelled", msg)] and msg.endswith("⭐ saved to golden set")


def test_cli_uri_failure_notifies_and_raises(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(label_cli, "notify", lambda t, m: sent.append((t, m)))
    with pytest.raises(ValueError):
        label_cli.handle_uri("jdlabel:golden/nope.md", str(tmp_path), "postgresql://t")
    assert sent and sent[0][0] == "Label failed"


def test_cli_remove_by_link_and_by_note(tmp_path, monkeypatch):
    rel = _note(tmp_path)
    seen = []
    monkeypatch.setattr(labels, "remove_label", lambda db, link, lab: seen.append((link, lab)) or 1)
    assert label_cli.remove("golden", LINK, str(tmp_path), "db") == "removed golden label"
    label_cli.remove("golden", rel, str(tmp_path), "db")
    assert seen == [(LINK, "golden"), (LINK, "golden")]


def test_cli_list_prints_rows(monkeypatch, capsys):
    row = {"title": "AI Engineer", "company": "Acme", "score": 9,
           "labeled_at": datetime(2026, 10, 11, 12, 0), "link": LINK}
    monkeypatch.setattr(labels, "list_labels", lambda db, lab: [row])
    monkeypatch.setenv("DATABASE_URL", "postgresql://t")
    assert label_cli.main(["--list", "golden"]) == 0
    assert capsys.readouterr().out.strip() == f"AI Engineer | Acme | 9 | 2026-10-11 | {LINK}"


def test_list_labels_joins_offers():
    conn, cur = _conn()
    cur.fetchall.return_value = [("T", "C", 8, datetime(2026, 1, 1), LINK)]
    with patch("src.labels.psycopg2.connect", return_value=conn):
        rows = labels.list_labels("postgresql://t", "golden")
    assert rows[0]["title"] == "T"
    sql = [c[0][0] for c in cur.execute.call_args_list][-1]
    assert "JOIN offers" in sql
    assert "LEFT JOIN jobs j ON j.id = l.job_id" in sql


def test_list_labels_falls_back_when_the_archive_jobs_table_is_absent():
    conn, cur = _conn(fetch=(False,))
    cur.fetchall.return_value = [("T", "C", 8, datetime(2026, 1, 1), LINK)]
    with patch("src.labels.psycopg2.connect", return_value=conn):
        rows = labels.list_labels("postgresql://t", "golden")
    assert rows[0]["title"] == "T"
    sql = [c[0][0] for c in cur.execute.call_args_list][-1]
    assert "JOIN offers" in sql and "LEFT JOIN jobs" not in sql


def test_main_usage_returns_1(capsys):
    assert label_cli.main([]) == 1


def test_cli_uri_names_the_label_it_replaced(tmp_path, monkeypatch):
    rel = _note(tmp_path)
    sent = []
    monkeypatch.setattr(label_cli, "notify", lambda t, m: sent.append((t, m)))
    monkeypatch.setattr(labels, "set_label", lambda *a: {"created": True, "replaced": "golden"})
    msg = label_cli.handle_uri(f"jdlabel:not_for_me/{rel}", str(tmp_path), "postgresql://t")
    assert msg.endswith("👎 saved to not-for-me set (replaced golden)")
    assert sent == [("Offer labelled", msg)]
