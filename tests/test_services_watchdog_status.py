from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.delivery.settings import DeliverySettings
from src.jobstore import budget, heartbeat
from src.jobstore.sightings import CardInput, record_sighting
from src.linkedin.schedule import ScheduleConfig, local_day
from src.services import status, watchdog

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def settings():
    return DeliverySettings(output_path="o", delivery_time=time(8), timezone=ZoneInfo("Europe/Rome"),
                            expected_services=("collect-linkedin", "process", "deliver"))


class Outbox:
    def __init__(self):
        self.sent = []

    def __call__(self, text):
        self.sent.append(text)
        return True


def test_enabled_sources_join_the_expected_services(settings):
    names = watchdog.expected_services(settings, ["freehire"])
    assert names == ("collect-linkedin", "process", "deliver", "collect-freehire")
    assert watchdog.expected_services(settings, []) == settings.expected_services


def test_enabled_sources_are_read_from_the_sources_config(tmp_path):
    path = tmp_path / "sources.json"
    path.write_text('{"freehire": {"enabled": true}, "himalayas": {"enabled": false}}')
    assert watchdog.enabled_sources(str(path)) == ["freehire"]
    assert watchdog.enabled_sources(str(tmp_path / "missing.json")) == []
    path.write_text("not json")
    assert watchdog.enabled_sources(str(path)) == []


def test_the_watchdog_alerts_on_a_silent_service(store_conn, store_conn_factory, settings, tmp_path):
    for name in settings.expected_services:
        heartbeat.start(store_conn, name, now=NOW)
        heartbeat.beat(store_conn, name, "running", now=NOW if name != "deliver" else NOW - timedelta(hours=2))
    record_sighting(store_conn, CardInput(source="linkedin", source_job_id="1", source_url="https://l/1",
                                          title="t", company="c"), NOW)
    store_conn.commit()
    outbox = Outbox()
    sent = watchdog.run(settings, outbox, connect_fn=lambda: store_conn_factory(store_conn),
                        state_path=str(tmp_path / "state.json"), now=NOW)
    assert len(sent) == 1 and "deliver has been silent for 2h00m" in sent[0]


def test_an_unreachable_store_is_reported_once_per_six_hours_and_the_recovery_is_announced(
        store_conn, store_conn_factory, settings, tmp_path):
    state = str(tmp_path / "state.json")
    outbox = Outbox()

    def down():
        raise ConnectionError("connection refused")

    assert len(watchdog.run(settings, outbox, connect_fn=down, state_path=state, now=NOW)) == 1
    assert watchdog.run(settings, outbox, connect_fn=down, state_path=state, now=NOW + timedelta(hours=1)) == []
    assert len(watchdog.run(settings, outbox, connect_fn=down, state_path=state,
                            now=NOW + timedelta(hours=6, minutes=1))) == 1
    for name in settings.expected_services:
        heartbeat.start(store_conn, name, now=NOW + timedelta(hours=7))
    record_sighting(store_conn, CardInput(source="linkedin", source_job_id="1", source_url="https://l/1",
                                          title="t", company="c"), NOW + timedelta(hours=7))
    store_conn.commit()
    back = watchdog.run(settings, outbox, connect_fn=lambda: store_conn_factory(store_conn), state_path=state,
                        now=NOW + timedelta(hours=7))
    assert back == ["OK again: the job store is reachable"]


def test_a_failed_down_alert_is_retried_not_remembered(settings, tmp_path):
    state = str(tmp_path / "state.json")

    def down():
        raise ConnectionError("refused")

    assert watchdog.run(settings, lambda text: False, connect_fn=down, state_path=state, now=NOW) == []
    assert len(watchdog.run(settings, Outbox(), connect_fn=down, state_path=state, now=NOW)) == 1


def test_source_silence_allowances_allow_the_interval_plus_slack(tmp_path):
    path = tmp_path / "sources.json"
    path.write_text('{"freehire": {"enabled": true, "interval_hours": 24}, '
                    '"himalayas": {"enabled": false, "interval_hours": 24}}')
    assert watchdog.source_silence_allowances(str(path)) == {"collect-freehire": timedelta(hours=27)}


def test_a_timer_collector_gets_its_interval_plus_slack_before_it_counts_as_silent(settings, store_conn):
    from src.delivery import health
    heartbeat.start(store_conn, "collect-freehire", now=NOW)
    heartbeat.beat(store_conn, "collect-freehire", "ok", now=NOW)
    store_conn.commit()
    expected = DeliverySettings(**{**settings.__dict__, "expected_services": ("collect-freehire",)})
    allowance = {"collect-freehire": timedelta(hours=27)}
    assert health.service_issues(store_conn, expected, NOW + timedelta(hours=20), allowance) == []
    issues = health.service_issues(store_conn, expected, NOW + timedelta(hours=40), allowance)
    assert len(issues) == 1 and "collect-freehire has been silent" in issues[0].message


def test_an_error_beat_shows_in_the_day_summary_line(settings, store_conn):
    from src.delivery import health
    heartbeat.start(store_conn, "collect-freehire", now=NOW)
    heartbeat.beat(store_conn, "collect-freehire", "error", {"slices": 8}, now=NOW)
    store_conn.commit()
    expected = DeliverySettings(**{**settings.__dict__, "expected_services": ("collect-freehire",)})
    lines = health.health_lines(store_conn, expected, NOW, {"collect-freehire": timedelta(hours=27)})
    assert lines == ["collect-freehire: error, last beat 0m ago, up 0m"]


def test_an_error_beat_is_an_issue_only_for_a_timer_collector(settings, store_conn):
    from src.delivery import health
    expected = DeliverySettings(**{**settings.__dict__, "expected_services": ("collect-freehire",)})
    heartbeat.start(store_conn, "collect-freehire", now=NOW)
    heartbeat.beat(store_conn, "collect-freehire", "error", {"slices": 8}, now=NOW)
    store_conn.commit()
    issues = health.service_issues(store_conn, expected, NOW, {"collect-freehire": timedelta(hours=27)})
    assert len(issues) == 1
    assert issues[0].message == "collect-freehire: last run ended in error {'slices': 8}"
    assert health.service_issues(store_conn, expected, NOW, {}) == []


def test_status_shows_stages_slices_pace_budget_and_services(store_conn):
    record_sighting(store_conn, CardInput(source="linkedin", source_job_id="1", source_url="https://l/1",
                                          title="t", company="c"), NOW)
    with store_conn.cursor() as cur:
        cur.execute("INSERT INTO slices (source, role, area, tier, base_interval_seconds, interval_seconds, "
                    "due_at) VALUES ('linkedin', 'AI Engineer', 'Italy', 1, 14400, 14400, %s)", (NOW,))
        cur.execute("INSERT INTO pace_budgets (host, next_slot_at, weekday_spacing_seconds) "
                    "VALUES ('linkedin.com', %s, 67.5)", (NOW,))
    budget.add_usage(store_conn, "openrouter", requests=7, now=NOW)
    heartbeat.start(store_conn, "process", now=NOW)
    store_conn.commit()
    text = status.render(store_conn, NOW)
    assert "tier -  seen" in text and "linkedin" in text
    assert "LinkedIn pace: base 68s weekday / 110s weekend, hour nudge x1.0, 0 refusals in a row" in text
    assert "linkedin   cadence: 4h x1" in text
    assert "openrouter today (UTC): 7 requests" in text and "process" in text


def test_status_shows_the_schedule_gate_count_and_window(store_conn):
    with store_conn.cursor() as cur:
        cur.execute("INSERT INTO pace_budgets (host, next_slot_at, requests_today, day) "
                    "VALUES ('linkedin.com', %s, 5, %s)", (NOW, NOW.date()))
    store_conn.commit()
    text = status.render(store_conn, NOW, ScheduleConfig())
    assert "LinkedIn gate: 5/1400 requests today (weekday), window open" in text


def test_status_shows_a_closed_quiet_window(store_conn):
    schedule = ScheduleConfig()
    night = datetime(2026, 10, 13, 23, 0, tzinfo=timezone.utc)  # Rome 01:00
    with store_conn.cursor() as cur:
        cur.execute("INSERT INTO pace_budgets (host, next_slot_at, requests_today, day) "
                    "VALUES ('linkedin.com', %s, 2, %s)", (night, local_day(schedule, night)))
    store_conn.commit()
    text = status.render(store_conn, night, schedule)
    assert "window closed until 06:00 Rome" in text

