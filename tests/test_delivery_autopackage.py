import json
import os
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import openai
import pytest

from src.delivery.autopackage import (
    PackagingConfig, candidates, classify_through_pace, package_day, run_packaging,
)
from src.delivery.jobs import load_jobs
from src.delivery.settings import DeliverySettings
from src.linkedin.pace import Gate, PaceGateClosed
from src.processor.config import load_processor_config
from tests.delivery_helpers import NOW, scored_job
from tests.linkedin_fakes import BusyPace, FakePace

DAY = date(2026, 10, 14)


@pytest.fixture(scope="module")
def cfg():
    return load_processor_config()


@pytest.fixture
def env(tmp_path):
    master, css = tmp_path / "CV_master.md", tmp_path / "CV_css.md"
    master.write_text("cv")
    css.write_text("css")
    settings = DeliverySettings(output_path=str(tmp_path / "out"), delivery_time=time(8, 0),
                                timezone=ZoneInfo("Europe/Rome"), telegram_token="t", telegram_chat_id="c")
    return settings, PackagingConfig(str(master), str(css), "llm-key")


class Tailor:
    def __init__(self, tmp_path, fail=None):
        self.calls, self.days, self.fail, self.tmp_path = [], [], fail or {}, tmp_path

    def __call__(self, note, root, master, css, key, package_day=None):
        self.calls.append(note)
        self.days.append(package_day)
        if note in self.fail:
            raise self.fail[note]
        directory = self.tmp_path / f"pkg{len(self.calls)}"
        directory.mkdir()
        return str(directory)


def _delivered(conn, n, score=9, **kw):
    return scored_job(conn, n, score, stage="delivered", note_day=DAY, **kw)


def test_package_day_predicts_the_next_digest_day_before_delivery(store_conn, env):
    # An undelivered job lands in today's digest while today's 08:00 delivery has not
    # started, and in tomorrow's once it has.
    settings, _ = env
    job_id = scored_job(store_conn, 1, 9, note_day=DAY,
                        alerted_at=datetime(2026, 10, 14, 5, 0, tzinfo=timezone.utc))  # 07:00 Rome
    job = load_jobs(store_conn, "j.id = %s", (job_id,))[0]
    assert package_day(job, settings, datetime(2026, 10, 14, 5, 0, tzinfo=timezone.utc)) == "2026-10-14"
    assert package_day(job, settings, NOW) == "2026-10-15"


def test_package_day_of_a_failed_alert_is_the_next_digest_not_the_note_day(store_conn, env):
    # The alert send failed at 10:00 Rome: the note is written under its note day but
    # the job is only delivered by the next morning's digest.
    settings, _ = env
    job_id = scored_job(store_conn, 1, 9, note_day=DAY)
    job = load_jobs(store_conn, "j.id = %s", (job_id,))[0]
    assert job.alerted_at is None
    assert package_day(job, settings, datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc)) == "2026-10-15"


def test_package_day_prefers_the_delivered_day(store_conn, env):
    # Once delivered, the actual digest day wins, covering the same-tick alert whose
    # alert time is at/after 08:00 but which delivery still put in that day's digest.
    settings, _ = env
    job_id = scored_job(store_conn, 1, 9, stage="delivered", note_day=DAY,
                        delivery_day=date(2026, 10, 14), alerted_at=NOW)
    job = load_jobs(store_conn, "j.id = %s", (job_id,))[0]
    assert package_day(job, settings, NOW + timedelta(days=1)) == "2026-10-14"


def test_run_packaging_passes_the_digest_day_to_the_tailor(store_conn, cfg, env, tmp_path):
    settings, pkg = env
    delivered = scored_job(store_conn, 1, 10, stage="delivered", note_day=DAY,
                           delivery_day=DAY, alerted_at=datetime(2026, 10, 14, 5, 0, tzinfo=timezone.utc))
    alerted = scored_job(store_conn, 2, 9, note_day=DAY, alerted_at=NOW)
    store_conn.commit()
    tailor = Tailor(tmp_path)
    results = run_packaging(store_conn, cfg, settings, pkg, FakePace(), now=NOW, tailor_run=tailor,
                            classify=lambda link: "external_ats")
    assert [r["job_id"] for r in results] == [delivered, alerted]
    assert tailor.days == ["2026-10-14", "2026-10-15"]


def test_candidates_are_confirmed_above_threshold_with_a_note_and_no_package_yet(store_conn, cfg):
    good = _delivered(store_conn, 1)
    _delivered(store_conn, 2, score=7)
    _delivered(store_conn, 3, verdict="unconfirmed")  # legacy: reads as not stated
    _delivered(store_conn, 7, verdict="advised", remote_advice="remote, limited to the UK - check eligibility")
    _delivered(store_conn, 8, verdict="not_stated")
    scored_job(store_conn, 4, 9)  # scored but no note written yet
    tier2 = _delivered(store_conn, 5, tier=2, verdict="not_checked", location="Zurich, Switzerland")
    packaged = _delivered(store_conn, 6)
    with store_conn.cursor() as cur:
        cur.execute("INSERT INTO job_applications (job_id, channel) VALUES (%s, 'unknown')", (packaged,))
    assert {j.id for j in candidates(store_conn, cfg, NOW)} == {good, tier2}


def test_candidates_skip_a_job_whose_packaging_failed_recently(store_conn, cfg):
    job = _delivered(store_conn, 1)
    with store_conn.cursor() as cur:
        cur.execute("INSERT INTO kv_state (key, value) VALUES (%s, %s::jsonb)",
                    (f"package_fail:{job}", json.dumps({"attempts": 1, "stopped": False,
                                                         "next_try_at": (NOW + timedelta(hours=1)).isoformat()})))
    store_conn.commit()
    assert candidates(store_conn, cfg, NOW) == []


def test_a_candidate_is_classified_tailored_recorded_and_not_notified_in_dry_run(store_conn, cfg, env, tmp_path):
    settings, pkg = env
    job = _delivered(store_conn, 1)
    tailor, notified = Tailor(tmp_path), []
    results = run_packaging(store_conn, cfg, settings, pkg, FakePace(), now=NOW, tailor_run=tailor,
                            classify=lambda link: "external_ats",
                            notify=lambda *a: notified.append(a))
    assert len(results) == 1 and results[0]["dry_run"] is True and notified == []
    assert tailor.calls[0].startswith(os.path.join(settings.output_path, "2026-10-14", "tier1", "scraped"))
    manifest = json.load(open(os.path.join(results[0]["directory"], "APPLICATION_PACKAGE.json")))
    assert manifest["channel"] == "external_ats" and manifest["dry_run"] is True
    with store_conn.cursor() as cur:
        cur.execute("SELECT channel, dry_run FROM job_applications WHERE job_id = %s", (job,))
        assert cur.fetchone() == ("external_ats", True)
        cur.execute("SELECT application_channel FROM jobs WHERE id = %s", (job,))
        assert cur.fetchone()[0] == "external_ats"
    assert run_packaging(store_conn, cfg, settings, pkg, FakePace(), now=NOW, tailor_run=tailor,
                         classify=lambda link: "x") == []  # never tailored twice


def test_live_mode_sends_the_package_ready_notice(store_conn, cfg, env, tmp_path):
    settings, pkg = env
    live = replace(cfg, policies={t: replace(p, autoapply_dry_run=False) for t, p in cfg.policies.items()})
    _delivered(store_conn, 1)
    notified = []
    run_packaging(store_conn, live, settings, pkg, FakePace(), now=NOW, tailor_run=Tailor(tmp_path),
                  classify=lambda link: "unknown", notify=lambda *a: notified.append(a))
    assert len(notified) == 1 and notified[0][3:] == ("t", "c")


def test_a_failed_tailoring_skips_the_job_and_continues(store_conn, cfg, env, tmp_path):
    settings, pkg = env
    first, second = _delivered(store_conn, 1, score=10), _delivered(store_conn, 2, score=9)
    from src.delivery.daily import note_path
    from src.delivery.jobs import load_jobs
    bad_note = note_path(settings, load_jobs(store_conn, "j.id = %s", (first,))[0], DAY)
    tailor = Tailor(tmp_path, fail={bad_note: ValueError("HALTED: missing metric")})
    results = run_packaging(store_conn, cfg, settings, pkg, FakePace(), now=NOW, tailor_run=tailor,
                            classify=lambda link: "unknown")
    assert [r["job_id"] for r in results] == [second]
    with store_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM job_applications WHERE job_id = %s", (first,))
        assert cur.fetchone()[0] == 0  # retried on the next pass


def test_an_openrouter_daily_quota_stops_the_loop(store_conn, cfg, env, tmp_path):
    settings, pkg = env
    _delivered(store_conn, 1, score=10)
    _delivered(store_conn, 2, score=9)
    quota = openai.RateLimitError(
        "quota", response=httpx.Response(429, request=httpx.Request("POST", "https://openrouter.ai")),
        body={"code": "token_quota_exceeded"})

    def tailor(note, *args, **kwargs):
        raise quota

    assert run_packaging(store_conn, cfg, settings, pkg, FakePace(), now=NOW, tailor_run=tailor,
                         classify=lambda link: "unknown") == []


def test_a_missing_cv_source_fails_before_anything_is_tailored(store_conn, cfg, env):
    settings, pkg = env
    _delivered(store_conn, 1)
    with pytest.raises(FileNotFoundError, match="CV_MASTER_PATH"):
        run_packaging(store_conn, cfg, settings, replace(pkg, cv_master_path="/nope"), FakePace(),
                      now=NOW, tailor_run=Tailor(None), classify=lambda link: "x")


def test_a_linkedin_classify_request_goes_through_the_shared_pace_but_other_hosts_do_not():
    pace = FakePace()
    assert classify_through_pace(pace, "https://www.linkedin.com/jobs/view/1", lambda l: "linkedin_easy_apply") \
        == "linkedin_easy_apply"
    assert pace.events == [("description", "clean")]
    assert classify_through_pace(pace, "https://boards.greenhouse.io/acme/1", lambda l: "external_ats") \
        == "external_ats"
    assert len(pace.events) == 1


class _ClosedPace:
    """A pace whose schedule gate is closed: every slot raises, no request happens."""

    def gate(self):
        return Gate(open=False, reason="daily_cap", requests_today=0, cap=1400, window="weekday")

    @contextmanager
    def slot(self, kind):
        raise PaceGateClosed(self.gate())
        yield


def test_a_closed_gate_defers_the_linkedin_candidate_and_packages_the_rest(store_conn, cfg, env, tmp_path):
    settings, pkg = env
    linkedin = _delivered(store_conn, 1, score=10)
    external = scored_job(store_conn, 2, 9, stage="delivered", note_day=DAY,
                          canonical_url="https://boards.greenhouse.io/acme/2")
    store_conn.commit()
    tailor, notified = Tailor(tmp_path), []
    results = run_packaging(store_conn, cfg, settings, pkg, _ClosedPace(), now=NOW, tailor_run=tailor,
                            classify=lambda link: "external_ats", notify=lambda *a: notified.append(a))
    assert [r["job_id"] for r in results] == [external]
    assert notified == []
    with store_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM job_applications WHERE job_id = %s", (linkedin,))
        assert cur.fetchone()[0] == 0  # still a candidate, not a failure
        cur.execute("SELECT count(*) FROM kv_state WHERE key LIKE 'package_fail:%%'")
        assert cur.fetchone()[0] == 0
    assert linkedin in {job.id for job in candidates(store_conn, cfg, NOW)}


def test_a_busy_pace_is_asked_once_per_packaging_pass(store_conn, cfg, env, tmp_path):
    settings, pkg = env
    first, second = _delivered(store_conn, 1, score=10), _delivered(store_conn, 2, score=9)
    external = scored_job(store_conn, 3, 8, stage="delivered", note_day=DAY,
                          canonical_url="https://boards.greenhouse.io/acme/3")
    store_conn.commit()
    pace = BusyPace()
    results = run_packaging(store_conn, cfg, settings, pkg, pace, now=NOW, tailor_run=Tailor(tmp_path),
                            classify=lambda link: "external_ats")
    assert pace.attempts == 1                               # the second LinkedIn candidate never asked
    assert [r["job_id"] for r in results] == [external]
    with store_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM kv_state WHERE key LIKE 'package_fail:%%'")
        assert cur.fetchone()[0] == 0                       # deferred, not failed
    assert {first, second} <= {job.id for job in candidates(store_conn, cfg, NOW)}


def test_build_service_gives_the_shared_pace_the_deliver_name_and_schedule(monkeypatch, cfg):
    import src.delivery.service as service_module
    import src.linkedin.pace as pace_module
    captured = {}

    class RecordingPace:
        def __init__(self, connect, **options):
            captured.update(options)

    monkeypatch.setattr(pace_module, "ManagedPace", RecordingPace)
    monkeypatch.setattr(service_module, "load_processor_config",
                        lambda: replace(cfg, policies={t: replace(p, autoapply_enabled=False)
                                                       for t, p in cfg.policies.items()}))
    monkeypatch.setattr(service_module, "load_settings",
                        lambda: DeliverySettings(output_path="o", delivery_time=time(8, 0),
                                                 timezone=ZoneInfo("Europe/Rome")))
    service_module.build_service()
    assert captured["service"] == "deliver"
    assert captured["schedule"].timezone == "Europe/Rome"
    assert captured["give_up_after"] == cfg.description_seconds_per_tick
