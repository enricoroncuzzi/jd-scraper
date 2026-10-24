import json
import subprocess
import sys
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.delivery.settings import DeliverySettings
from src.jobstore import budget, heartbeat
from src.jobstore.sightings import CardInput, record_sighting
from src.jobstore.source_requests import record_request
from src.linkedin.adaptive import blank_state
from src.linkedin.schedule import AdaptivePolicy, ScheduleConfig, local_day
from src.processor.cheap import check_one
from src.processor.config import load_processor_config
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
    assert "LinkedIn pace: adaptive level 1/5 (75s, 1x), 0/20 refused in trailing 20, 0/0 refused today, 0 refusals in a row" in text
    assert "linkedin   cadence: 4h x1" in text
    assert "openrouter today (UTC): 7 requests" in text and "process" in text


def test_status_shows_the_schedule_gate_count_and_window(store_conn):
    with store_conn.cursor() as cur:
        cur.execute("INSERT INTO pace_budgets (host, next_slot_at, requests_today, day) "
                    "VALUES ('linkedin.com', %s, 5, %s)", (NOW, NOW.date()))
    store_conn.commit()
    text = status.render(store_conn, NOW, ScheduleConfig())
    assert "LinkedIn gate: 5 requests today, window open" in text


def test_status_shows_a_restored_closed_quiet_window(store_conn):
    schedule = ScheduleConfig(closed_hours=(0, 6))
    night = datetime(2026, 10, 13, 23, 0, tzinfo=timezone.utc)  # Rome 01:00
    with store_conn.cursor() as cur:
        cur.execute("INSERT INTO pace_budgets (host, next_slot_at, requests_today, day) "
                    "VALUES ('linkedin.com', %s, 2, %s)", (night, local_day(schedule, night)))
    store_conn.commit()
    text = status.render(store_conn, night, schedule)
    assert "window closed until 06:00 Rome" in text


def test_status_shows_the_gate_open_at_night_without_a_closed_window(store_conn):
    schedule = ScheduleConfig()
    night = datetime(2026, 10, 13, 23, 0, tzinfo=timezone.utc)  # Rome 01:00
    with store_conn.cursor() as cur:
        cur.execute("INSERT INTO pace_budgets (host, next_slot_at, requests_today, day) "
                    "VALUES ('linkedin.com', %s, 2, %s)", (night, local_day(schedule, night)))
    store_conn.commit()
    text = status.render(store_conn, night, schedule)
    assert "LinkedIn gate: 2 requests today, window open" in text


def test_status_does_not_show_yesterdays_refusals_as_today(store_conn):
    schedule = ScheduleConfig()
    night = datetime(2026, 10, 13, 23, 0, tzinfo=timezone.utc)  # Rome 01:00
    yesterday = local_day(schedule, night) - timedelta(days=1)
    with store_conn.cursor() as cur:
        cur.execute("INSERT INTO pace_budgets (host, next_slot_at, requests_today, refusals_today, day) "
                    "VALUES ('linkedin.com', %s, 830, 120, %s)", (night, yesterday))
    store_conn.commit()
    text = status.render(store_conn, night, schedule)
    assert "0/0 refused today" in text
    assert "LinkedIn gate: 0 requests today" in text



def test_every_status_time_is_converted_to_rome_and_labelled(store_conn):
    import re
    with store_conn.cursor() as cur:
        cur.execute("SET TIME ZONE 'America/New_York'")  # the server zone must not leak
        cur.execute("INSERT INTO jobs (canonical_url, title, company, company_norm, title_norm, "
                    "first_seen_at) VALUES ('https://l/2', 't', 'c', 'c', 't', %s)", (NOW,))
        cur.execute("INSERT INTO slices (source, role, area, tier, base_interval_seconds, "
                    "interval_seconds, last_walked_at, due_at) "
                    "VALUES ('linkedin', 'AI Engineer', 'Italy', 1, 43200, 43200, %s, %s)", (NOW, NOW))
        cur.execute("INSERT INTO pace_budgets (host, next_slot_at, last_clean_at, cooldown_until) "
                    "VALUES ('linkedin.com', %s, %s, %s)",
                    (NOW, NOW, NOW + timedelta(minutes=4)))
    heartbeat.start(store_conn, "process", now=NOW + timedelta(seconds=5))
    store_conn.commit()
    text = status.render(store_conn, NOW + timedelta(minutes=1), ScheduleConfig())
    assert "2026-10-14 14:00 Rome" in text and "2026-10-14 14:00:05 Rome" in text
    assert "waiting until 14:04:00 Rome" in text
    assert all(zone == "Rome" for zone in re.findall(r"\d{2}:\d{2}(?::\d{2})? (\w+)", text))


def test_status_shows_the_description_queue_split_by_priority_class(store_conn):
    cfg = load_processor_config()
    for n, (title, location) in enumerate([("AI Engineer", "Italy"), ("AI Engineer", "Milan, Italy"),
                                           ("Python Developer", "Italy"), ("Python Developer", "Milan, Italy")]):
        job = record_sighting(store_conn, CardInput(source="linkedin", source_job_id=str(n),
                                                    source_url=f"https://l/{n}", title=title, company="c",
                                                    location=location, tier_hint=1), NOW).job_id
        check_one(store_conn, job, cfg, NOW)
        if n == 0:
            with store_conn.cursor() as cur:
                cur.execute("UPDATE jobs SET description_class = NULL WHERE id = %s", (job,))
    store_conn.commit()
    text = status.render(store_conn, NOW, ScheduleConfig())
    assert "1 pick in 4 is oldest-first" in text
    assert "ai_country 0" in text and "unclassified 1" in text
    assert "other_city 1" in text and "(no waiting descriptions)" in text


def test_status_shows_a_rank_left_over_from_an_older_class_list(store_conn):
    # A config change (a shorter class list) leaves ranks that no class covers. They must
    # stay visible, or the reclassification step in the RUNBOOK looks like it worked.
    cfg = load_processor_config()
    job = record_sighting(store_conn, CardInput(source="linkedin", source_job_id="9",
                                                source_url="https://l/9", title="AI Engineer",
                                                company="c", location="Italy", tier_hint=1), NOW).job_id
    check_one(store_conn, job, cfg, NOW)
    with store_conn.cursor() as cur:
        cur.execute("UPDATE jobs SET description_class = 9 WHERE id = %s", (job,))
    store_conn.commit()
    text = status.render(store_conn, NOW, ScheduleConfig())
    assert "class9 1" in text


def test_status_keeps_the_dashboard_when_the_processor_config_is_broken(store_conn, monkeypatch):
    # A malformed description_order block stops jds-process and jds-deliver at start; the
    # operator view must still show the gate, the budgets and the heartbeats.
    def _broken():
        raise ValueError("description_order.quota_one_in must be an integer >= 1, got 0")
    monkeypatch.setattr(status, "load_processor_config", _broken)
    with store_conn.cursor() as cur:
        cur.execute("INSERT INTO pace_budgets (host, next_slot_at) VALUES ('linkedin.com', %s)", (NOW,))
    heartbeat.start(store_conn, "process", now=NOW)
    store_conn.commit()
    text = status.render(store_conn, NOW, ScheduleConfig())
    assert "description_order.quota_one_in" in text
    assert "LinkedIn gate:" in text and "Services:" in text and "process" in text


def _llm_call(conn, stage, model, at=NOW):
    from src.llm_fallback import STAGE_PRIMARIES
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO llm_calls (id, stage, provider, request_model, response_model, latency_ms, "
            "batch_size, attempt, outcome, prompt_version, started_at) "
            "VALUES (gen_random_uuid(), %s, 'openrouter', %s, %s, 1000, 1, 1, 'ok', 'v', %s)",
            (stage, STAGE_PRIMARIES[stage], model, at))
    conn.commit()


def test_status_shows_what_answered_each_stage_today(store_conn):
    from src.scorer import _OPENROUTER_MODEL as primary
    for _ in range(3):
        _llm_call(store_conn, "scoring", primary)
    for _ in range(2):
        _llm_call(store_conn, "scoring", "liquid/lfm-2.5-2.6b:free")
    text = status.render(store_conn, NOW, ScheduleConfig())
    assert "OpenRouter calls today (Rome)" in text
    scoring = next(line for line in text.splitlines() if "scoring" in line and "calls" in line)
    assert "5 calls" in scoring and "60% by the primary" in scoring
    assert "liquid/lfm-2.5-2.6b:free (2)" in scoring


def test_the_watchdog_alerts_when_a_fallback_model_answers_a_stage(
        store_conn, store_conn_factory, settings, tmp_path):
    for name in settings.expected_services:
        heartbeat.start(store_conn, name, now=NOW)
        heartbeat.beat(store_conn, name, "running", now=NOW)
    for _ in range(10):
        _llm_call(store_conn, "scoring", "liquid/lfm-2.5-2.6b:free")
    outbox = Outbox()
    sent = watchdog.run(settings, outbox, connect_fn=lambda: store_conn_factory(store_conn),
                        state_path=str(tmp_path / "state.json"), now=NOW)
    assert len(sent) == 1
    assert "scoring" in sent[0] and "liquid/lfm-2.5-2.6b:free" in sent[0]


def test_a_failing_guard_does_not_stop_the_health_check(
        store_conn, store_conn_factory, settings, tmp_path, monkeypatch, capsys):
    from src import llm_fallback
    for name in settings.expected_services:
        heartbeat.start(store_conn, name, now=NOW)
        heartbeat.beat(store_conn, name, "running",
                       now=NOW if name != "deliver" else NOW - timedelta(hours=2))
    store_conn.commit()

    def boom(*args, **kwargs):
        raise RuntimeError("the guard exploded")
    monkeypatch.setattr(llm_fallback, "run_guard", boom)

    path = tmp_path / "state.json"
    path.write_text(json.dumps({"store_down_sent_at": NOW.isoformat()}))
    outbox = Outbox()
    sent = watchdog.run(settings, outbox, connect_fn=lambda: store_conn_factory(store_conn),
                        state_path=str(path), now=NOW)
    # The health alerts still went out and the store-down note still cleared.
    assert len(sent) == 2
    assert "deliver has been silent for 2h00m" in sent[0]
    assert sent[1] == "OK again: the job store is reachable"
    assert not path.exists()
    assert "fallback guard failed: RuntimeError: the guard exploded" in capsys.readouterr().err


def test_the_watchdog_start_up_does_not_load_the_llm_stack():
    # The watchdog is the safety net: an import failure in the LLM stack (a renamed
    # pin, a dependency missing between git pull and pip install) must not stop it.
    script = (
        "import sys, src.services.watchdog; print(sorted(m for m in "
        "('src.llm_fallback', 'src.scorer', 'src.remote_verifier', 'src.tailor.generate', "
        "'openai', 'langchain_core') if m in sys.modules))"
    )
    result = subprocess.run([sys.executable, "-c", script], cwd=".", capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


ACTIVE = ScheduleConfig(adaptive=AdaptivePolicy(enabled=True))


def _render_adaptive(store_conn, state, schedule=ACTIVE):
    with store_conn.cursor() as cur:
        cur.execute("INSERT INTO pace_budgets (host, next_slot_at, adaptive_state) VALUES "
                    "('linkedin.com', %s, %s::jsonb) ON CONFLICT (host) DO UPDATE "
                    "SET adaptive_state = EXCLUDED.adaptive_state", (NOW, json.dumps(state)))
    store_conn.commit()
    return status.render(store_conn, NOW, schedule)


def test_the_status_line_shows_the_level_the_last_move_and_the_next_climb(store_conn):
    state = {**blank_state(ACTIVE.adaptive), "active": True, "level": 2,
             "trailing": ["clean"] * 18 + ["refused"] * 2,
             "levels": {"2": {"requests": 40, "refusals": 1, "successes": 39,
                               "active_seconds": 2000.0, "first_at": None,
                               "last_at": None, "cooldown_hours": 2.0,
                               "blocked_until": None}},
             "last_move": {"at": NOW.isoformat(), "kind": "climb", "from": 1, "to": 2,
                            "reason": "climb: cautious upper 11.4%, 43/h vs 14/h"}}
    text = _render_adaptive(store_conn, state)
    assert "LinkedIn pace: adaptive level 2/5 (50s, 1.5x)" in text
    assert "2/20 refused in trailing 20" in text
    assert "last move: climb 1 -> 2" in text
    assert "next climb: 40/200 requests at this level" in text


def test_the_status_line_shows_a_blocked_target_level(store_conn):
    state = {**blank_state(ACTIVE.adaptive), "active": True, "level": 2,
             "signals": {"demand": 5000, "llm": 100, "llm_day": "2026-10-14",
                          "llm_stop": 800, "at": NOW.isoformat()},
             "levels": {"2": {"requests": 200, "refusals": 0, "successes": 200,
                               "active_seconds": 15000.0, "first_at": None, "last_at": None,
                               "cooldown_hours": 2.0, "blocked_until": None},
                        "3": {"requests": 0, "refusals": 0, "successes": 0,
                               "active_seconds": 0.0, "first_at": None, "last_at": None,
                               "cooldown_hours": 2.0,
                               "blocked_until": (NOW + timedelta(hours=2)).isoformat()}}}
    text = _render_adaptive(store_conn, state)
    assert "next climb: level 3 blocked until" in text


def test_the_status_line_shows_the_emergency_brake(store_conn):
    state = {**blank_state(ACTIVE.adaptive), "active": True, "level": 1,
             "brake": {"since": (NOW - timedelta(minutes=5)).isoformat(),
                       "reason": "http_999", "clean": 0}}
    text = _render_adaptive(store_conn, state)
    assert "adaptive emergency 150s" in text and "climbing frozen" in text


def test_the_status_line_shows_the_paused_gate(store_conn):
    state = {**blank_state(ACTIVE.adaptive), "active": True, "level": 1,
             "paused": {"day": "2026-10-14", "reason": "http_999"}}
    text = _render_adaptive(store_conn, state)
    assert "LinkedIn gate: 0 requests today, LinkedIn paused for the rest of the day" in text


def test_the_status_keeps_the_drop_reason_after_more_refusals_at_level_1(store_conn):
    state = {**blank_state(ACTIVE.adaptive), "active": True, "level": 1,
             "trailing": ["refused"] * 10,
             "last_move": {"at": NOW.isoformat(), "kind": "drop", "from": 3, "to": 1,
                            "reason": "protect: trailing 20 refused 30%"}}
    text = _render_adaptive(store_conn, state)
    assert "last move: drop 3 -> 1" in text
    assert "protect at floor" not in text


def test_status_renders_corrupt_state(store_conn):
    for schedule in (ACTIVE, ScheduleConfig()):
        for bad in ({"paused": 3}, {"paused": {"day": 5}}, {"level": "x"}, "nope", {}):
            assert "LinkedIn pace:" in _render_adaptive(store_conn, bad, schedule)


def test_the_status_line_shows_the_off_switch(store_conn):
    with store_conn.cursor() as cur:
        cur.execute("INSERT INTO pace_budgets (host, next_slot_at) VALUES ('linkedin.com', %s)", (NOW,))
    store_conn.commit()
    text = status.render(store_conn, NOW, ScheduleConfig())
    assert ("LinkedIn pace: adaptive off, fixed 75s, 0/20 refused in trailing 20, "
            "0/0 refused today, 0 refusals in a row") in text
    assert "last move: disabled" in text


def test_the_status_page_shows_the_last_seven_days_of_requests_per_source(store_conn):
    for code in (200, 200, 429):
        with store_conn.cursor() as cur:
            cur.execute("INSERT INTO pace_events (at, service, kind, outcome, status) "
                        "VALUES (%s, 'collect-linkedin', 'search', %s, %s)",
                        (NOW - timedelta(hours=2), "ok" if code == 200 else "refused", code))
    record_sighting(store_conn, CardInput(source="linkedin", source_job_id="l1",
                                          source_url="https://l/1", title="t", company="c"),
                    NOW - timedelta(hours=2))
    record_request(store_conn, "himalayas", NOW - timedelta(hours=2), 200)
    store_conn.commit()
    text = status.render(store_conn, NOW, ScheduleConfig())
    assert "Requests per source, last 7 days (Rome):" in text
    created = [line.split() for line in text.splitlines() if line.startswith("  linkedin  ")]
    # Today is included and the window is exactly seven Rome days.
    assert [row[1] for row in created] == [f"2026-10-{day:02d}" for day in range(8, 15)]
    assert created[-1] == ["linkedin", "2026-10-14", "3", "2", "1", "0", "1",
                           "200:2", "429:1"]
    himalayas = [line.split() for line in text.splitlines() if line.startswith("  himalayas ")]
    assert himalayas[-1] == ["himalayas", "2026-10-14", "1", "1", "0", "0", "0", "200:1"]
    freehire = [line.split() for line in text.splitlines() if line.startswith("  freehire  ")]
    assert freehire[0] == ["freehire", "2026-10-08", "0", "0", "0", "0", "0", "-"]
