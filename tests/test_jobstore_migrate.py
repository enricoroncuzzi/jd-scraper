from datetime import datetime, timedelta, timezone

import pytest

from src.jobstore.migrate import (
    is_title_only_fallback, legacy_key, map_description_status, run_migration,
)
from tests.legacy_schema import add_offer, add_run, create_legacy_tables

T0 = datetime(2026, 10, 1, 7, 30, tzinfo=timezone.utc)
LINK_A = "https://www.linkedin.com/jobs/view/ai-engineer-at-acme-3911111111"
LINK_A_OTHER_SLUG = "https://it.linkedin.com/jobs/view/ai-engineer-acme-3911111111"
LINK_B = "https://www.linkedin.com/jobs/view/ml-engineer-at-beta-3922222222"


@pytest.fixture
def legacy(store_conn):
    create_legacy_tables(store_conn)
    return store_conn


def _count(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


def test_legacy_key_uses_the_linkedin_id_so_slugs_and_hosts_do_not_matter():
    assert legacy_key(LINK_A) == legacy_key(LINK_A_OTHER_SLUG) == "3911111111"
    assert legacy_key("https://example.com/job/1").startswith("legacy:")


def test_title_only_fallback_detection():
    assert is_title_only_fallback("AI Engineer", "Acme", "AI Engineer at Acme")
    assert not is_title_only_fallback("AI Engineer", "Acme", "A real description of the job.")


@pytest.mark.parametrize("status,description,fallback,expected", [
    ("ok", "text", False, "ok"),
    ("partial", "meta description text", False, "ok"),
    ("partial", "AI Engineer at Acme", True, "failed"),
    ("failed", "", False, "failed"),
    ("ok", "   ", False, "failed"),
])
def test_map_description_status(status, description, fallback, expected):
    assert map_description_status(status, description, fallback) == expected


def test_each_posting_becomes_one_job_and_every_row_is_counted(legacy):
    run1, run2 = add_run(legacy, 1, T0), add_run(legacy, 1, T0 + timedelta(days=1))
    add_offer(legacy, run1, LINK_A, when=T0, score=6, description="Old text")
    add_offer(legacy, run2, LINK_A_OTHER_SLUG, when=T0 + timedelta(days=1), score=9,
              description="New text")
    add_offer(legacy, run1, LINK_B, when=T0, title="ML Engineer", company="Beta S.r.l.", tier=3,
              location="Berlin, Germany")
    legacy.commit()

    report = run_migration(legacy, legacy, dry_run=False)

    assert report.problems() == []
    assert (report.legacy_rows, report.distinct_postings, report.distinct_links) == (3, 2, 3)
    assert (report.jobs_created, report.sightings_created, report.seen_count_total) == (2, 2, 3)
    assert _count(legacy, "SELECT count(*) FROM jobs") == 2
    with legacy.cursor() as cur:
        cur.execute("SELECT score, description, stage, stage_reason, first_seen_at, last_seen_at, "
                    "delivery_day FROM jobs WHERE canonical_url = %s", (LINK_A_OTHER_SLUG,))
        score, description, stage, reason, first, last, day = cur.fetchone()
        cur.execute("SELECT seen_count FROM sightings WHERE source_job_id = '3911111111'")
        seen_count = cur.fetchone()[0]
    assert (score, description) == (9, "New text")
    assert (stage, reason) == ("delivered", "migrated")
    assert (first, last) == (T0, T0 + timedelta(days=1))
    assert str(day) == "2026-10-02"
    assert seen_count == 2


def test_title_only_fallback_rows_are_marked_failed(legacy):
    run = add_run(legacy, 1, T0)
    add_offer(legacy, run, LINK_A, when=T0, description="AI Engineer at Acme", status="partial")
    legacy.commit()
    report = run_migration(legacy, legacy, dry_run=False)
    assert report.fallback_marked_failed == 1
    with legacy.cursor() as cur:
        cur.execute("SELECT description_status FROM jobs")
        assert cur.fetchone()[0] == "failed"


def test_a_dry_run_changes_nothing(legacy):
    add_offer(legacy, add_run(legacy, 1, T0), LINK_A, when=T0)
    legacy.commit()
    report = run_migration(legacy, legacy, dry_run=True)
    assert report.jobs_created == 1
    assert _count(legacy, "SELECT count(*) FROM jobs") == 0


def test_running_twice_creates_no_duplicates(legacy):
    add_offer(legacy, add_run(legacy, 1, T0), LINK_A, when=T0)
    legacy.commit()
    run_migration(legacy, legacy, dry_run=False)
    second = run_migration(legacy, legacy, dry_run=False)
    assert second.jobs_created == 0 and second.already_migrated == 1
    assert second.problems() == []
    assert _count(legacy, "SELECT count(*) FROM jobs") == 1


def test_applications_are_rekeyed_and_unmatched_ones_get_a_minimal_job(legacy):
    run = add_run(legacy, 1, T0)
    add_offer(legacy, run, LINK_A, when=T0)
    with legacy.cursor() as cur:
        cur.execute("INSERT INTO applications (link_hash, link, title, company, channel, packaged_at, "
                    "dry_run) VALUES ('a', %s, 'AI Engineer', 'Acme', 'external_ats', %s, true)",
                    (LINK_A_OTHER_SLUG, T0))
        cur.execute("INSERT INTO applications (link_hash, link, title, company, channel, packaged_at, "
                    "dry_run) VALUES ('b', %s, 'Old Role', 'Gone Co', 'unknown', %s, false)",
                    ("https://www.linkedin.com/jobs/view/old-role-3999999999", T0))
    legacy.commit()

    report = run_migration(legacy, legacy, dry_run=False)

    assert (report.applications_total, report.applications_matched,
            report.applications_minimal_jobs) == (2, 1, 1)
    assert report.problems() == []
    assert _count(legacy, "SELECT count(*) FROM job_applications") == 2
    assert _count(legacy, "SELECT count(*) FROM job_applications WHERE dry_run") == 1


def test_labels_are_counted_and_unmatched_ones_fail_the_report(legacy):
    run = add_run(legacy, 1, T0)
    add_offer(legacy, run, LINK_A, when=T0)
    with legacy.cursor() as cur:
        cur.execute("INSERT INTO offer_labels (posting_key, link, label) VALUES "
                    "('linkedin:3911111111', %s, 'golden')", (LINK_A,))
        cur.execute("INSERT INTO offer_labels (posting_key, link, label) VALUES "
                    "('linkedin:3888888888', %s, 'not_for_me')",
                    ("https://www.linkedin.com/jobs/view/x-3888888888",))
    legacy.commit()

    report = run_migration(legacy, legacy, dry_run=False)

    assert (report.labels_total, report.labels_matched) == (2, 1)
    assert report.problems() == ["1 label(s) match no migrated job"]
    assert _count(legacy, "SELECT count(*) FROM jobs") == 0  # a failing report is not committed


def test_seen_hashes_are_loaded_per_tier_file(legacy, tmp_path):
    (tmp_path / "seen_tier1.txt").write_text("a" * 32 + "\n" + "b" * 32 + "\n\n")
    (tmp_path / "seen_tier3.txt").write_text("b" * 32 + "\n" + "not-a-hash\n")
    legacy.commit()
    report = run_migration(legacy, legacy, seen_dir=str(tmp_path), dry_run=False)
    assert report.seen_hashes_loaded == 2
    assert _count(legacy, "SELECT count(*) FROM legacy_seen_hashes") == 2


def test_cli_dry_run_prints_the_report_and_exits_zero(legacy, store_conn_factory, capsys):
    from src.jobstore.migrate_cli import main

    add_offer(legacy, add_run(legacy, 1, T0), LINK_A, when=T0)
    legacy.commit()
    code = main(["--neon-url", "postgresql://ignored"],
                open_legacy=lambda _url: store_conn_factory(legacy),
                open_store=lambda: store_conn_factory(legacy))
    out = capsys.readouterr().out
    assert code == 0
    assert "jobs created:               1" in out and "DRY RUN" in out
    assert _count(legacy, "SELECT count(*) FROM jobs") == 0


def test_cli_exits_one_when_the_fallback_count_differs(legacy, store_conn_factory, capsys):
    from src.jobstore.migrate_cli import main

    add_offer(legacy, add_run(legacy, 1, T0), LINK_A, when=T0)
    legacy.commit()
    code = main(["--neon-url", "x", "--expect-fallback", "38"],
                open_legacy=lambda _url: store_conn_factory(legacy),
                open_store=lambda: store_conn_factory(legacy))
    assert code == 1
    assert "expected 38" in capsys.readouterr().out
