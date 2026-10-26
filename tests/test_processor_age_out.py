from dataclasses import replace
from datetime import timedelta

import pytest

from src.processor import age_out
from src.processor.describe import expire_stale
from tests.test_processor_describe import NOW, _age, _state, _tiered_job, cfg  # noqa: F401


def _row(conn, job_id):
    with conn.cursor() as cur:
        cur.execute("SELECT stage, stage_reason, tier, description_class, description_status, "
                    "first_seen_at, posted_at, description, score, remote_verdict, "
                    "next_attempt_at, archived_at FROM jobs WHERE id = %s", (job_id,))
        return dict(zip([column[0] for column in cur.description], cur.fetchone()))


def test_the_dry_run_counts_by_tier_and_class_and_writes_nothing(store_conn, cfg, capsys):
    jobs = [
        _tiered_job(store_conn, cfg, "3900000001", title="Python Developer",
                    location="Berlin, Germany", tier_hint=3),
        _tiered_job(store_conn, cfg, "3900000002", title="Python Developer",
                    location="Munich, Germany", tier_hint=3),
        _tiered_job(store_conn, cfg, "3900000003", title="Python Developer",
                    location="London, United Kingdom", tier_hint=4),
        _tiered_job(store_conn, cfg, "3900000004", title="Python Developer",
                    location="Paris, France", tier_hint=3),
    ]
    _age(store_conn, {jobs[0]: 100, jobs[1]: 100, jobs[2]: 100, jobs[3]: 20})
    assert age_out.main(["--dry-run"], conn=store_conn, cfg=cfg, now=NOW) == 0
    out = capsys.readouterr().out
    assert "  tier 3 class 3 (other_city): 2" in out
    assert "  tier 4 class 3 (other_city): 1" in out
    assert "  total: 3" in out
    assert all(_state(store_conn, job)[0] == "seen" for job in jobs)


def test_the_dry_run_matches_what_the_next_tick_expires(store_conn, cfg):
    jobs = [_tiered_job(store_conn, cfg, f"390000000{n}", title="Python Developer",
                        location="Berlin, Germany", tier_hint=3) for n in (1, 2, 3)]
    _age(store_conn, {job: 100 for job in jobs})
    counted = sum(count for _tier, _rank, count in age_out.dry_run(store_conn, cfg, NOW))
    assert counted == expire_stale(store_conn, cfg, NOW)


def test_the_age_out_undo_restores_the_job_exactly(store_conn, cfg, capsys):
    job = _tiered_job(store_conn, cfg, "3900000001", title="Python Developer",
                      location="Berlin, Germany", tier_hint=3)
    _age(store_conn, {job: 100})
    with store_conn.cursor() as cur:                # a backoff and a Neon copy the age-out must not eat
        cur.execute("UPDATE jobs SET next_attempt_at = %s, archived_at = %s WHERE id = %s",
                    (NOW + timedelta(hours=6), NOW, job))
    store_conn.commit()
    before = _row(store_conn, job)                  # after the age, before the expiry
    expire_stale(store_conn, cfg, NOW)
    assert _state(store_conn, job)[:2] == ("expired", "aged_out")
    assert age_out.main(["--undo"], conn=store_conn, cfg=cfg, now=NOW) == 0
    after = _row(store_conn, job)
    assert "Restored 1 jobs from aged_out to seen" in capsys.readouterr().out
    assert after["stage"] == "seen" and after["stage_reason"] == ""
    for column in ("tier", "description_class", "description_status", "first_seen_at",
                   "posted_at", "description", "score", "remote_verdict", "next_attempt_at"):
        assert after[column] == before[column], column
    assert after["archived_at"] is None


def test_the_undo_works_while_the_rule_is_switched_off(store_conn, cfg):
    job = _tiered_job(store_conn, cfg, "3900000001", title="Python Developer",
                      location="Berlin, Germany", tier_hint=3)
    _age(store_conn, {job: 100})
    expire_stale(store_conn, cfg, NOW)
    off = replace(cfg, age_out_enabled=False)
    assert age_out.main(["--undo"], conn=store_conn, cfg=off, now=NOW) == 0
    assert _state(store_conn, job)[0] == "seen"


def test_the_dry_run_reports_a_switched_off_rule(store_conn, cfg, capsys):
    off = replace(cfg, age_out_enabled=False)
    assert age_out.main(["--dry-run"], conn=store_conn, cfg=off, now=NOW) == 0
    out = capsys.readouterr().out
    assert "age-out is disabled" in out and "  total: 0" in out


def test_the_dry_run_and_the_undo_together_are_refused(store_conn, cfg):
    with pytest.raises(SystemExit):
        age_out.main(["--dry-run", "--undo"], conn=store_conn, cfg=cfg, now=NOW)
