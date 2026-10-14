import os
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

from src.delivery.alerts import due_alerts, format_alert, run_alerts
from src.delivery.jobs import load_jobs
from src.delivery.settings import DeliverySettings
from src.processor.config import load_processor_config
from tests.delivery_helpers import NOW, scored_job


@pytest.fixture(scope="module")
def cfg():
    return load_processor_config()


@pytest.fixture
def settings(tmp_path):
    return DeliverySettings(output_path=str(tmp_path), delivery_time=time(8, 0), timezone=ZoneInfo("Europe/Rome"))


class Outbox:
    def __init__(self, fail_times=0):
        self.messages, self.fail_times = [], fail_times

    def __call__(self, text):
        if self.fail_times:
            self.fail_times -= 1
            raise ConnectionError("telegram down")
        self.messages.append(text)
        return True


def _row(conn, job_id):
    with conn.cursor() as cur:
        cur.execute("SELECT alerted_at, note_day, stage FROM jobs WHERE id = %s", (job_id,))
        return cur.fetchone()


def test_only_jobs_at_or_above_the_threshold_that_were_not_alerted_are_due(store_conn, cfg):
    strong = scored_job(store_conn, 1, 8)
    scored_job(store_conn, 2, 7)
    scored_job(store_conn, 3, 9, alerted_at=NOW)
    scored_job(store_conn, 4, 10, stage="delivered")
    assert [j.id for j in due_alerts(store_conn, cfg)] == [strong]


def test_an_alert_writes_the_note_into_todays_folder_and_sends_a_short_message(store_conn, cfg, settings):
    job = scored_job(store_conn, 1, 9, reason="Fully remote.")
    outbox = Outbox()
    assert run_alerts(store_conn, cfg, settings, outbox, NOW) == 1
    folder = os.path.join(settings.output_path, "2026-10-14", "tier1", "scraped")
    (name,) = os.listdir(folder)
    text = open(os.path.join(folder, name), encoding="utf-8").read()
    assert "score: 9" in text and "source: linkedin" in text
    assert outbox.messages == ["Strong match 9/10 - tier 1\nAI Engineer 1 - Acme 1\nSummary 1.\n"
                               "https://www.linkedin.com/jobs/view/job-1"]
    alerted_at, note_day, stage = _row(store_conn, job)
    assert (alerted_at, note_day, stage) == (NOW, date(2026, 10, 14), "scored")


def test_a_job_is_alerted_once(store_conn, cfg, settings):
    scored_job(store_conn, 1, 9)
    outbox = Outbox()
    run_alerts(store_conn, cfg, settings, outbox, NOW)
    assert run_alerts(store_conn, cfg, settings, outbox, NOW) == 0 and len(outbox.messages) == 1


def test_a_failed_send_keeps_the_job_due_and_the_note_where_it_was_put(store_conn, cfg, settings):
    job = scored_job(store_conn, 1, 9)
    assert run_alerts(store_conn, cfg, settings, Outbox(fail_times=1), NOW) == 0
    assert _row(store_conn, job)[0] is None and _row(store_conn, job)[1] == date(2026, 10, 14)
    later = datetime(2026, 10, 15, 6, 0, tzinfo=timezone.utc)
    outbox = Outbox()
    assert run_alerts(store_conn, cfg, settings, outbox, later) == 1
    assert _row(store_conn, job)[1] == date(2026, 10, 14)  # still the day the note was written


def test_a_send_that_returns_false_counts_as_failed(store_conn, cfg, settings):
    scored_job(store_conn, 1, 9)
    assert run_alerts(store_conn, cfg, settings, lambda text: False, NOW) == 0


def test_unconfirmed_remote_is_said_in_the_message(store_conn, cfg):
    scored_job(store_conn, 1, 9, verdict="unconfirmed")
    (job,) = load_jobs(store_conn, "j.stage = 'scored'")
    assert "(remote not confirmed)" in format_alert(job).splitlines()[0]


def test_packaging_runs_after_the_alert_and_its_failure_is_contained(store_conn, cfg, settings, capsys):
    job = scored_job(store_conn, 1, 9)
    called = []

    def boom(alerted):
        called.append(alerted.id)
        raise RuntimeError("chromium missing")

    assert run_alerts(store_conn, cfg, settings, Outbox(), NOW, on_alerted=boom) == 1
    assert called == [job] and "packaging after the alert failed" in capsys.readouterr().out
    assert _row(store_conn, job)[0] == NOW
