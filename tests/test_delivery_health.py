from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.delivery.health import (
    collection_issue, evaluate_health, health_lines, run_health_check, service_issues,
)
from src.delivery.settings import DeliverySettings
from src.jobstore import heartbeat
from src.jobstore.sightings import CardInput, record_sighting

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def settings():
    return DeliverySettings(output_path="out", delivery_time=time(8, 0), timezone=ZoneInfo("Europe/Rome"),
                            expected_services=("collect-linkedin", "process", "deliver"))


def _all_alive(conn, at=NOW):
    for name in ("collect-linkedin", "process", "deliver"):
        heartbeat.start(conn, name, now=at)
        heartbeat.beat(conn, name, "running", now=at)
    record_sighting(conn, CardInput(source="linkedin", source_job_id="1", source_url="https://l/1",
                                    title="t", company="c"), at)
    conn.commit()


class Outbox:
    def __init__(self, ok=True):
        self.messages, self.ok = [], ok

    def __call__(self, text):
        self.messages.append(text)
        return self.ok


def test_a_healthy_system_has_no_issues(store_conn, settings):
    _all_alive(store_conn)
    assert evaluate_health(store_conn, settings, NOW) == []


def test_a_service_silent_for_over_an_hour_is_down(store_conn, settings):
    _all_alive(store_conn)
    heartbeat.beat(store_conn, "process", "running", now=NOW - timedelta(minutes=61))
    (issue,) = service_issues(store_conn, settings, NOW)
    assert issue.key == "service:process" and "silent for 1h01m" in issue.message
    heartbeat.beat(store_conn, "process", "running", now=NOW - timedelta(minutes=59))
    assert service_issues(store_conn, settings, NOW) == []


def test_a_service_that_never_reported_is_an_issue(store_conn, settings):
    assert {i.key for i in service_issues(store_conn, settings, NOW)} == {
        "service:collect-linkedin", "service:process", "service:deliver"}


def test_nothing_collected_for_six_hours_is_an_issue_and_five_is_not(store_conn, settings):
    _all_alive(store_conn, NOW - timedelta(hours=7))
    assert collection_issue(store_conn, NOW).message == "Nothing has been collected for 7h00m"
    assert collection_issue(store_conn, NOW - timedelta(hours=2)) is None


def test_an_empty_store_reports_that_nothing_was_ever_collected(store_conn):
    assert collection_issue(store_conn, NOW).message == "Nothing has been collected ever"


def _refresh(conn, at, skip=()):
    """Everything is alive at `at` except the services in `skip`."""
    for name in ("collect-linkedin", "process", "deliver"):
        if name not in skip:
            heartbeat.beat(conn, name, "running", now=at)
    record_sighting(conn, CardInput(source="linkedin", source_job_id="1", source_url="https://l/1",
                                    title="t", company="c"), at)
    conn.commit()


def test_an_alert_is_sent_once_repeats_after_six_hours_and_clears_with_a_note(store_conn, settings):
    _all_alive(store_conn)
    heartbeat.beat(store_conn, "process", "running", now=NOW - timedelta(hours=2))
    store_conn.commit()
    outbox = Outbox()
    assert len(run_health_check(store_conn, settings, outbox, NOW)) == 1
    _refresh(store_conn, NOW + timedelta(hours=1), skip=("process",))
    assert run_health_check(store_conn, settings, outbox, NOW + timedelta(hours=1)) == []
    again = NOW + timedelta(hours=6, minutes=1)
    _refresh(store_conn, again, skip=("process",))
    assert len(run_health_check(store_conn, settings, outbox, again)) == 1
    back = NOW + timedelta(hours=7)
    _refresh(store_conn, back)
    cleared = run_health_check(store_conn, settings, outbox, back)
    assert cleared == ["OK again: service process is healthy"]
    assert run_health_check(store_conn, settings, outbox, back + timedelta(hours=1)) == []


def test_a_failed_send_is_retried_at_the_next_check(store_conn, settings):
    _all_alive(store_conn)
    heartbeat.beat(store_conn, "deliver", "running", now=NOW - timedelta(hours=3))
    store_conn.commit()
    assert run_health_check(store_conn, settings, Outbox(ok=False), NOW) == []
    assert len(run_health_check(store_conn, settings, Outbox(), NOW + timedelta(minutes=5))) == 1


def test_health_lines_show_every_service(store_conn, settings):
    _all_alive(store_conn)
    heartbeat.beat(store_conn, "process", "idle", now=NOW - timedelta(minutes=2))
    store_conn.commit()
    lines = health_lines(store_conn, settings, NOW)
    assert len(lines) == 3 and any(l.startswith("process: idle, last beat 2m ago") for l in lines)
    heartbeat.beat(store_conn, "deliver", "idle", now=NOW - timedelta(hours=3))
    assert any(l.startswith("deliver: DOWN") for l in health_lines(store_conn, settings, NOW))
