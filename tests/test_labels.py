import urllib.parse
import uuid
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

import label as label_cli
from src import labels
from src.delivery.archive import backfill_label_job_ids
from src.jobstore.merge import merge_jobs
from src.jobstore.sightings import CardInput, record_sighting
from src.storage import init_db
from tests.delivery_helpers import NOW
from tests.legacy_schema import add_offer, add_run, create_legacy_tables

LINK = "https://it.linkedin.com/jobs/view/ai-engineer-at-acme-3912345678"
ELEVEN_LABS_LINK = (
    "https://uk.linkedin.com/jobs/view/applied-ai-engineer-at-elevenlabs-4482146365"
)


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
    cur.fetchall.side_effect = [[], [("T", "C", 8, datetime(2026, 1, 1), LINK)]]
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


def _label_db_url(store_conn, base_url):
    """A DATABASE_URL that names the fixture's throwaway schema, so the public
    set_label/list_labels entry points can be exercised against a real database."""
    with store_conn.cursor() as cur:
        cur.execute("SHOW search_path")
        schema = cur.fetchone()[0].split(",")[0]
    sep = "&" if "?" in base_url else "?"
    return base_url + sep + "options=" + urllib.parse.quote(f"-c search_path={schema}", safe="")


def _eleven_labs_job(conn, *, with_linkedin_sighting=True):
    """The ElevenLabs shape: a freehire sighting makes the Ashby URL canonical, and
    the later LinkedIn sighting is merged into it. Returns (job_id, note_no).

    with_linkedin_sighting=False models the label database (Neon) that never received
    the LinkedIn sighting because the job was archived before it arrived."""
    create_legacy_tables(conn)
    with conn.cursor() as cur:
        # The migration in set_label adds this on a live database.
        cur.execute("ALTER TABLE offer_labels ADD COLUMN IF NOT EXISTS job_id UUID")
    job_id = record_sighting(conn, CardInput(
        source="freehire", source_job_id="elevenlabs-applied-ai-engineer",
        source_url="https://freehire.me/jobs/elevenlabs-applied-ai-engineer",
        canonical_url="https://jobs.ashbyhq.com/elevenlabs/"
                      "b8a111fa-9fd5-44d0-97ed-8fd1280be4ef?utm_source=freehire.me",
        title="Applied AI Engineer", company="ElevenLabs", location="London, United Kingdom",
        description="We build voice AI.", description_source="freehire"), NOW).job_id
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET tier = 4, score = 10, stage = 'scored', "
                    "remote_verdict = 'confirmed' WHERE id = %s", (job_id,))
    if with_linkedin_sighting:
        linkedin = record_sighting(conn, CardInput(
            source="linkedin", source_job_id="4482146365", source_url=ELEVEN_LABS_LINK,
            title="Applied AI Engineer", company="ElevenLabs",
            location="London, United Kingdom"), NOW).job_id
        job_id = merge_jobs(conn, job_id, linkedin)
    with conn.cursor() as cur:
        cur.execute("SELECT note_no, canonical_url FROM jobs WHERE id = %s", (job_id,))
        note_no, canonical_url = cur.fetchone()
    conn.commit()
    assert canonical_url.startswith("https://jobs.ashbyhq.com/")
    return job_id, note_no


def test_set_label_links_the_job_behind_a_different_canonical_url(store_conn, store_url_fixture):
    job_id, note_no = _eleven_labs_job(store_conn)
    labels.set_label(_label_db_url(store_conn, store_url_fixture), ELEVEN_LABS_LINK, "golden",
                     f"elevenlabs_applied_ai_engineer_{note_no}.md")
    with store_conn.cursor() as cur:
        cur.execute("SELECT job_id FROM offer_labels WHERE label = 'golden'")
        assert str(cur.fetchone()[0]) == str(job_id)


def test_set_label_falls_back_on_the_note_number_when_the_sighting_is_missing(
        store_conn, store_url_fixture):
    job_id, note_no = _eleven_labs_job(store_conn, with_linkedin_sighting=False)
    labels.set_label(_label_db_url(store_conn, store_url_fixture), ELEVEN_LABS_LINK, "golden",
                     f"elevenlabs_applied_ai_engineer_{note_no}.md")
    with store_conn.cursor() as cur:
        cur.execute("SELECT job_id FROM offer_labels WHERE label = 'golden'")
        assert str(cur.fetchone()[0]) == str(job_id)


def test_listing_repairs_a_label_that_lacks_a_job_id(store_conn, store_url_fixture):
    job_id, note_no = _eleven_labs_job(store_conn, with_linkedin_sighting=False)
    with store_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO offer_labels (posting_key, link, label, note) VALUES (%s, %s, 'golden', %s)",
            (labels.posting_key(ELEVEN_LABS_LINK), ELEVEN_LABS_LINK,
             f"elevenlabs_applied_ai_engineer_{note_no}.md"))
    store_conn.commit()
    rows = labels.list_labels(_label_db_url(store_conn, store_url_fixture), "golden")
    assert rows[0]["title"] == "Applied AI Engineer" and rows[0]["score"] == 10
    assert rows[0]["company"] == "ElevenLabs"
    with store_conn.cursor() as cur:
        cur.execute("SELECT job_id FROM offer_labels WHERE label = 'golden'")
        assert str(cur.fetchone()[0]) == str(job_id)


def test_a_job_id_already_set_is_never_overwritten(store_conn, store_url_fixture):
    job_id, note_no = _eleven_labs_job(store_conn, with_linkedin_sighting=False)
    other = uuid.uuid4()
    note = f"elevenlabs_applied_ai_engineer_{note_no}.md"
    with store_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO offer_labels (posting_key, link, label, job_id, note) "
            "VALUES (%s, %s, 'golden', %s::uuid, %s)",
            (labels.posting_key(ELEVEN_LABS_LINK), ELEVEN_LABS_LINK, str(other), note))
    store_conn.commit()
    labels.list_labels(_label_db_url(store_conn, store_url_fixture), "golden")
    labels.set_label(_label_db_url(store_conn, store_url_fixture), ELEVEN_LABS_LINK, "golden", note)
    with store_conn.cursor() as cur:
        cur.execute("SELECT job_id FROM offer_labels WHERE label = 'golden'")
        assert str(cur.fetchone()[0]) == str(other)


def test_a_note_number_collision_does_not_link_an_unrelated_job(store_conn, store_url_fixture):
    """A legacy note also ends in a small offer id (src/writer.py::_note_filename), so a
    note number alone is not identity: the filename must name the job's company."""
    _job_id, note_no = _eleven_labs_job(store_conn, with_linkedin_sighting=False)
    legacy_link = "https://it.linkedin.com/jobs/view/ai-engineer-at-acme-3912345678"
    legacy_note = f"acme_ai_engineer_{note_no}.md"
    with store_conn.cursor() as cur:
        cur.execute("INSERT INTO offer_labels (posting_key, link, label, note) "
                    "VALUES (%s, %s, 'golden', %s)",
                    (labels.posting_key(legacy_link), legacy_link, legacy_note))
    store_conn.commit()
    url = _label_db_url(store_conn, store_url_fixture)
    assert backfill_label_job_ids(store_conn) == 0
    labels.list_labels(url, "golden")
    labels.set_label(url, legacy_link, "golden", legacy_note)
    with store_conn.cursor() as cur:
        cur.execute("SELECT job_id FROM offer_labels WHERE link = %s", (legacy_link,))
        assert cur.fetchone()[0] is None


def test_a_label_matched_to_a_legacy_offer_skips_the_note_fallback(store_conn, store_url_fixture):
    """When the legacy offers table already identified the posting, the note number is
    not needed and must not second-guess it."""
    _job_id, note_no = _eleven_labs_job(store_conn, with_linkedin_sighting=False)
    add_offer(store_conn, add_run(store_conn, 4, NOW), ELEVEN_LABS_LINK, when=NOW,
              title="Applied AI Engineer", company="ElevenLabs")
    store_conn.commit()
    labels.set_label(_label_db_url(store_conn, store_url_fixture), ELEVEN_LABS_LINK, "golden",
                     f"elevenlabs_applied_ai_engineer_{note_no}.md")
    with store_conn.cursor() as cur:
        cur.execute("SELECT offer_id, job_id FROM offer_labels WHERE label = 'golden'")
        offer_id, job_id = cur.fetchone()
    assert offer_id is not None and job_id is None


def test_cli_uri_names_the_label_it_replaced(tmp_path, monkeypatch):
    rel = _note(tmp_path)
    sent = []
    monkeypatch.setattr(label_cli, "notify", lambda t, m: sent.append((t, m)))
    monkeypatch.setattr(labels, "set_label", lambda *a: {"created": True, "replaced": "golden"})
    msg = label_cli.handle_uri(f"jdlabel:not_for_me/{rel}", str(tmp_path), "postgresql://t")
    assert msg.endswith("👎 saved to not-for-me set (replaced golden)")
    assert sent == [("Offer labelled", msg)]
