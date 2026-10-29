from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import json
import pytest

from src.delivery.health import (
    evaluate_health, health_lines, linkedin_blocked_issue, run_health_check, service_issues,
)
from src.delivery.settings import DeliverySettings, load_settings
from src.jobstore import heartbeat
from src.jobstore.sightings import CardInput, record_sighting
from src.linkedin.adaptive import blank_state
from src.linkedin.schedule import AdaptivePolicy, ScheduleConfig

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


def _last_clean(conn, at):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO pace_budgets (host, next_slot_at, last_clean_at) "
                    "VALUES ('linkedin.com', %s, %s)", (at, at))
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


# -- the 24 h-of-open-time LinkedIn alert ------------------------------------------------

def test_no_linkedin_success_for_24_open_hours_alerts(store_conn, settings):
    _last_clean(store_conn, datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc))  # Wed 14:00 Rome
    thu_14 = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)                # Thu 14:00 Rome, +24 h
    issue = linkedin_blocked_issue(store_conn, settings, thu_14)
    assert issue.key == "linkedin_blocked" and "24h00m of open time" in issue.message
    assert linkedin_blocked_issue(store_conn, settings, thu_14 - timedelta(minutes=1)) is None


def test_without_a_closed_window_open_time_is_wall_time(store_conn, settings):
    _last_clean(store_conn, datetime(2026, 10, 14, 21, 0, tzinfo=timezone.utc))  # Wed 23:00 Rome
    # 24.5 h of wall clock is 24.5 h of open time: the night no longer pauses LinkedIn.
    thu_2330 = datetime(2026, 10, 15, 21, 30, tzinfo=timezone.utc)               # Thu 23:30 Rome
    issue = linkedin_blocked_issue(store_conn, settings, thu_2330)
    assert issue is not None and "24h30m of open time" in issue.message


def test_a_restored_closed_window_does_not_count_toward_the_block(store_conn, settings):
    _last_clean(store_conn, datetime(2026, 10, 14, 21, 0, tzinfo=timezone.utc))  # Wed 23:00 Rome
    # The revert path: 24.5 h of wall clock, but only 18.5 h open (Wed 23-24, Thu 06-23:30).
    thu_2330 = datetime(2026, 10, 15, 21, 30, tzinfo=timezone.utc)               # Thu 23:30 Rome
    assert linkedin_blocked_issue(store_conn, settings, thu_2330,
                                  ScheduleConfig(closed_hours=(0, 6))) is None


def test_the_block_alert_is_sent_at_most_once_a_day(store_conn, settings):
    _last_clean(store_conn, NOW - timedelta(days=3))
    for at, expected in ((NOW, True), (NOW + timedelta(hours=6), False),
                         (NOW + timedelta(hours=23), False), (NOW + timedelta(hours=24), True)):
        _all_alive(store_conn, at)
        sent = run_health_check(store_conn, settings, Outbox(), at)
        assert bool([m for m in sent if "LinkedIn" in m]) is expected, at


def test_the_shipped_deliver_config_alerts_after_24_open_hours_and_repeats_daily():
    settings = load_settings()
    assert (settings.linkedin_blocked_after_open_hours, settings.linkedin_blocked_repeat_hours) == (24, 24)


def test_a_store_that_never_reached_linkedin_does_not_alert(store_conn, settings):
    assert linkedin_blocked_issue(store_conn, settings, NOW) is None


def test_stale_sightings_alone_no_longer_alert(store_conn, settings):
    _all_alive(store_conn)
    _last_clean(store_conn, NOW - timedelta(hours=1))
    assert evaluate_health(store_conn, settings, NOW) == []


def test_the_old_collection_alert_is_cleared_once_after_deploy(store_conn, settings):
    _all_alive(store_conn)
    with store_conn.cursor() as cur:
        cur.execute("INSERT INTO kv_state (key, value) VALUES ('health:collection', %s::jsonb)",
                    (json.dumps({"sent_at": (NOW - timedelta(days=1)).isoformat()}),))
    store_conn.commit()
    assert run_health_check(store_conn, settings, Outbox(), NOW) == ["OK again: collection is healthy"]
    with store_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM kv_state WHERE key = 'health:collection'")
        assert cur.fetchone()[0] == 0


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

ACTIVE = ScheduleConfig(adaptive=AdaptivePolicy(enabled=True))


def _paused_today(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO pace_budgets (host, next_slot_at, adaptive_state) VALUES "
                    "('linkedin.com', %s, %s::jsonb)", (NOW, json.dumps({
                        **blank_state(), "active": True, "level": 1,
                        "paused": {"day": "2026-10-14", "reason": "http_999"}})))
    conn.commit()


def test_a_pause_today_is_a_health_issue(store_conn, settings):
    _paused_today(store_conn)
    _all_alive(store_conn)
    issues = {i.key: i for i in evaluate_health(store_conn, settings, NOW, schedule=ACTIVE)}
    assert "linkedin_paused" in issues
    assert "paused for the rest of the day" in issues["linkedin_paused"].message
    assert "http_999" in issues["linkedin_paused"].message


def test_the_pause_issue_clears_the_next_rome_day(store_conn, settings):
    _paused_today(store_conn)
    _all_alive(store_conn)
    tomorrow = NOW + timedelta(days=1)
    assert "linkedin_paused" not in {
        i.key for i in evaluate_health(store_conn, settings, tomorrow, schedule=ACTIVE)}


def test_a_pause_with_the_ladder_off_is_not_a_health_issue(store_conn, settings):
    _paused_today(store_conn)
    _all_alive(store_conn)
    assert "linkedin_paused" not in {
        i.key for i in evaluate_health(store_conn, settings, NOW, schedule=ScheduleConfig())}


def test_a_missing_pace_row_is_not_a_pause_issue(store_conn, settings):
    _all_alive(store_conn)
    assert "linkedin_paused" not in {
        i.key for i in evaluate_health(store_conn, settings, NOW, schedule=ACTIVE)}


def test_corrupt_pause_state_is_not_a_health_issue(store_conn, settings):
    for bad in ('{"paused": 3}', '{"paused": {"day": 5}}', '{"level": "x"}', '"nope"'):
        with store_conn.cursor() as cur:
            cur.execute("INSERT INTO pace_budgets (host, next_slot_at, adaptive_state) VALUES "
                        "('linkedin.com', %s, %s::jsonb) ON CONFLICT (host) DO UPDATE "
                        "SET adaptive_state = EXCLUDED.adaptive_state", (NOW, bad))
        store_conn.commit()
        _all_alive(store_conn, NOW)
        assert "linkedin_paused" not in {
            i.key for i in evaluate_health(store_conn, settings, NOW, schedule=ACTIVE)}


def test_the_pause_alert_repeats_at_most_once_a_day(store_conn, settings):
    _paused_today(store_conn)
    _all_alive(store_conn)
    outbox = Outbox()
    sent = run_health_check(store_conn, settings, outbox, NOW, schedule=ACTIVE)
    assert len([m for m in sent if "LinkedIn paused" in m]) == 1
    _all_alive(store_conn, NOW + timedelta(hours=6))
    assert run_health_check(store_conn, settings, outbox, NOW + timedelta(hours=6),
                            schedule=ACTIVE) == []
    _all_alive(store_conn, NOW + timedelta(days=1))
    cleared = run_health_check(store_conn, settings, outbox, NOW + timedelta(days=1),
                               schedule=ACTIVE)
    assert cleared == ["OK again: linkedin_paused is healthy"]


def test_the_pause_alert_is_retried_after_a_failed_send(store_conn, settings):
    _paused_today(store_conn)
    _all_alive(store_conn)
    assert run_health_check(store_conn, settings, Outbox(ok=False), NOW,
                            schedule=ACTIVE) == []
    sent = run_health_check(store_conn, settings, Outbox(), NOW + timedelta(minutes=5),
                            schedule=ACTIVE)
    assert len([m for m in sent if "LinkedIn paused" in m]) == 1


# -- the "went quiet" checks ------------------------------------------------------------
# Three silent failures the other alerts cannot see: LinkedIn answers requests but yields
# no new job (a layout change), a model stage makes calls nobody answers (every model
# failing, which the fallback guard leaves out by counting answers only), and nothing
# reaches the scored stage at all. Each alerts at most once a day.

def _pace_event(conn, at, outcome="ok"):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO pace_events (at, service, kind, outcome) "
                    "VALUES (%s, 'collect-linkedin', 'search', %s)", (at, outcome))
    conn.commit()


def _llm_call(conn, stage, at, model="nvidia/nemotron-3-ultra-550b-a55b:free", outcome="ok"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO llm_calls (id, stage, provider, request_model, response_model, latency_ms, "
            "batch_size, attempt, outcome, prompt_version, started_at) "
            "VALUES (gen_random_uuid(), %s, 'openrouter', 'requested', %s, 1000, 1, 1, %s, 'v', %s)",
            (stage, model, outcome, at))
    conn.commit()


def _reached_scored(conn, at, stage="scored"):
    job_id = record_sighting(conn, CardInput(source="linkedin", source_job_id=f"s-{at.isoformat()}",
                                            source_url=f"https://l/{at.isoformat()}", title="t",
                                            company="c"), at).job_id
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET stage = %s, stage_changed_at = %s WHERE id = %s",
                    (stage, at, job_id))
    conn.commit()
    return job_id


def _quiet_but_healthy(conn, at):
    """Everything a healthy store shows at `at`: beats, a new LinkedIn job, successful
    requests, an answered model call and a job that just reached scored."""
    _refresh(conn, at)
    _pace_event(conn, at - timedelta(hours=1))
    _llm_call(conn, "scoring", at - timedelta(hours=1))
    _reached_scored(conn, at - timedelta(hours=5))


def test_a_quiet_healthy_day_has_no_issues(store_conn, settings):
    _quiet_but_healthy(store_conn, NOW)
    assert evaluate_health(store_conn, settings, NOW) == []


# no new LinkedIn job in 24 h while requests succeed

def test_no_new_linkedin_job_for_24h_while_requests_succeed_alerts(store_conn, settings):
    _quiet_but_healthy(store_conn, NOW)
    # The last new job is 25 h old, but the collector is still getting clean answers.
    with store_conn.cursor() as cur:
        cur.execute("UPDATE sightings SET seen_at = %s", (NOW - timedelta(hours=25),))
    store_conn.commit()
    _pace_event(store_conn, NOW - timedelta(hours=2))
    issues = {i.key: i for i in evaluate_health(store_conn, settings, NOW)}
    assert "linkedin_no_new_jobs" in issues
    assert "no new job in 25h00m while requests still succeed" in issues["linkedin_no_new_jobs"].message


def test_a_linkedin_job_23h_ago_is_not_an_issue(store_conn, settings):
    _quiet_but_healthy(store_conn, NOW)
    with store_conn.cursor() as cur:
        cur.execute("UPDATE sightings SET seen_at = %s", (NOW - timedelta(hours=23),))
    store_conn.commit()
    _pace_event(store_conn, NOW - timedelta(minutes=1))
    assert evaluate_health(store_conn, settings, NOW) == []


def test_a_night_of_no_new_jobs_is_not_an_issue(store_conn, settings):
    # The shipped config has no closed window, so 02:00 Rome with the last new job at 18:00
    # is eight quiet hours, not a stall: the alert needs a full day of them.
    night = datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc)   # 02:00 Rome
    for name in ("collect-linkedin", "process", "deliver"):
        heartbeat.start(store_conn, name, now=night)
        heartbeat.beat(store_conn, name, "running", now=night)
    record_sighting(store_conn, CardInput(source="linkedin", source_job_id="1", source_url="https://l/1",
                                         title="t", company="c"), night - timedelta(hours=8))
    store_conn.commit()
    _pace_event(store_conn, night - timedelta(minutes=10))
    _llm_call(store_conn, "scoring", night - timedelta(minutes=10))
    _reached_scored(store_conn, night - timedelta(hours=5))
    assert evaluate_health(store_conn, settings, night) == []


def test_no_new_jobs_while_every_request_is_refused_is_left_to_the_block_alert(store_conn, settings):
    # A blocked or paused LinkedIn needs no second alert: with no successful request in
    # the window, the new-jobs check stays quiet and the block/pause alert owns it.
    _quiet_but_healthy(store_conn, NOW)
    with store_conn.cursor() as cur:
        cur.execute("UPDATE sightings SET seen_at = %s", (NOW - timedelta(hours=25),))
    store_conn.commit()
    with store_conn.cursor() as cur:
        cur.execute("DELETE FROM pace_events")
    store_conn.commit()
    _pace_event(store_conn, NOW - timedelta(hours=2), outcome="refused")
    assert "linkedin_no_new_jobs" not in {i.key for i in evaluate_health(store_conn, settings, NOW)}


def test_a_linkedin_pause_day_does_not_add_a_new_jobs_alert(store_conn, settings):
    # The pause is already alerted; a pause day stops the requests, so nothing succeeds
    # and the new-jobs check has nothing to contradict.
    _paused_today(store_conn)
    _quiet_but_healthy(store_conn, NOW)
    with store_conn.cursor() as cur:
        cur.execute("DELETE FROM pace_events")
        cur.execute("UPDATE sightings SET seen_at = %s", (NOW - timedelta(hours=30),))
    store_conn.commit()
    _pace_event(store_conn, NOW - timedelta(hours=5), outcome="refused")
    keys = {i.key for i in evaluate_health(store_conn, settings, NOW, schedule=ACTIVE)}
    assert keys == {"linkedin_paused"}


def test_a_store_that_never_saw_a_linkedin_job_does_not_alert(store_conn, settings):
    _quiet_but_healthy(store_conn, NOW)
    with store_conn.cursor() as cur:
        cur.execute("DELETE FROM sightings")
    store_conn.commit()
    _pace_event(store_conn, NOW - timedelta(minutes=5))
    assert "linkedin_no_new_jobs" not in {i.key for i in evaluate_health(store_conn, settings, NOW)}


def test_the_new_jobs_alert_is_sent_at_most_once_a_day(store_conn, settings):
    _quiet_but_healthy(store_conn, NOW)
    with store_conn.cursor() as cur:
        cur.execute("UPDATE sightings SET seen_at = %s", (NOW - timedelta(hours=25),))
    store_conn.commit()
    outbox = Outbox()
    for at, expected in ((NOW, True), (NOW + timedelta(hours=6), False),
                         (NOW + timedelta(hours=23), False), (NOW + timedelta(hours=24), True)):
        _pace_event(store_conn, at - timedelta(minutes=1))
        _refresh(store_conn, at)
        with store_conn.cursor() as cur:
            cur.execute("UPDATE sightings SET seen_at = %s", (NOW - timedelta(hours=25),))
        store_conn.commit()
        sent = run_health_check(store_conn, settings, outbox, at)
        assert bool([m for m in sent if "no new job in" in m]) is expected, at


# a model stage with calls but no answers, for 6 h since its last answer

def test_a_stage_with_calls_but_no_answers_alerts(store_conn, settings):
    _quiet_but_healthy(store_conn, NOW)
    _llm_call(store_conn, "verification", NOW - timedelta(hours=7), model=None, outcome="error")
    _llm_call(store_conn, "verification", NOW - timedelta(hours=1), model=None, outcome="error")
    issues = {i.key: i for i in evaluate_health(store_conn, settings, NOW)}
    assert "llm_stage:verification" in issues
    assert "made 2 model call(s) in the last 7h00m" in issues["llm_stage:verification"].message
    assert "none produced a usable answer" in issues["llm_stage:verification"].message


def test_a_stage_that_only_gets_empty_answers_alerts(store_conn, settings):
    # A provider can answer on time with a well-formed but empty or invalid payload (the
    # dots-3 `{"offers": []}` pattern). The call names a model, so `response_model` is set,
    # but the stage produced nothing usable: that is a dead stage, not an answer.
    _quiet_but_healthy(store_conn, NOW)
    with store_conn.cursor() as cur:
        cur.execute("DELETE FROM llm_calls")
    store_conn.commit()
    for _ in range(3):
        _llm_call(store_conn, "verification", NOW - timedelta(hours=7), outcome="invalid_output")
    issues = {i.key: i for i in evaluate_health(store_conn, settings, NOW)}
    assert "llm_stage:verification" in issues
    assert "made 3 model call(s)" in issues["llm_stage:verification"].message


def test_one_later_answer_is_enough(store_conn, settings):
    _quiet_but_healthy(store_conn, NOW)
    _llm_call(store_conn, "verification", NOW - timedelta(hours=7), model=None, outcome="error")
    _llm_call(store_conn, "verification", NOW - timedelta(minutes=30))
    assert {i.key for i in evaluate_health(store_conn, settings, NOW)} == set()


def test_one_failed_call_after_an_idle_stretch_is_not_yet_an_issue(store_conn, settings):
    # Tailoring last answered 20 h ago only because it had nothing to do; one 429 an hour
    # ago is an hour of failing, not six.
    _quiet_but_healthy(store_conn, NOW)
    _llm_call(store_conn, "tailoring", NOW - timedelta(hours=20))
    _llm_call(store_conn, "tailoring", NOW - timedelta(hours=1), model=None, outcome="rate_limited")
    assert {i.key for i in evaluate_health(store_conn, settings, NOW)} == set()


def test_a_silent_stage_stays_an_issue_while_it_makes_no_calls(store_conn, settings):
    # Tailoring failed 10 h ago and has made no call since: nothing has answered, so the
    # stage is still silent rather than healthy again.
    _quiet_but_healthy(store_conn, NOW)
    _llm_call(store_conn, "tailoring", NOW - timedelta(hours=10), model=None, outcome="error")
    issues = {i.key: i for i in evaluate_health(store_conn, settings, NOW)}
    assert "llm_stage:tailoring" in issues
    assert "made 1 model call(s) in the last 10h00m" in issues["llm_stage:tailoring"].message


def test_a_silent_stage_does_not_recover_without_an_answer(store_conn, settings):
    _quiet_but_healthy(store_conn, NOW)
    _llm_call(store_conn, "tailoring", NOW - timedelta(hours=6), model=None, outcome="error")
    outbox = Outbox()
    assert [m for m in run_health_check(store_conn, settings, outbox, NOW) if "tailoring stage" in m]
    later = NOW + timedelta(hours=7)
    _quiet_but_healthy(store_conn, later)
    assert not [m for m in run_health_check(store_conn, settings, outbox, later) if "tailoring" in m]
    _llm_call(store_conn, "tailoring", later + timedelta(minutes=5), model=None, outcome="error")
    assert not [m for m in run_health_check(store_conn, settings, outbox, later + timedelta(minutes=10))
                if "tailoring" in m]
    _llm_call(store_conn, "tailoring", later + timedelta(minutes=15))
    assert [m for m in run_health_check(store_conn, settings, outbox, later + timedelta(minutes=20))
            if "OK again: llm_stage tailoring is healthy" in m]


def test_each_silent_stage_gets_its_own_issue(store_conn, settings):
    _quiet_but_healthy(store_conn, NOW)
    with store_conn.cursor() as cur:
        cur.execute("DELETE FROM llm_calls")
    store_conn.commit()
    _llm_call(store_conn, "scoring", NOW - timedelta(hours=7), model=None, outcome="error")
    _llm_call(store_conn, "tailoring", NOW - timedelta(hours=7), model=None, outcome="error")
    keys = {i.key for i in evaluate_health(store_conn, settings, NOW)}
    assert keys == {"llm_stage:scoring", "llm_stage:tailoring"}


def test_the_stage_alert_is_sent_at_most_once_a_day(store_conn, settings):
    _quiet_but_healthy(store_conn, NOW)
    _llm_call(store_conn, "verification", NOW - timedelta(hours=7), model=None, outcome="error")
    outbox = Outbox()
    for hours, expected in ((0, True), (6, False), (23, False), (24, True)):
        at = NOW + timedelta(hours=hours)
        _refresh(store_conn, at)
        _llm_call(store_conn, "verification", at - timedelta(minutes=5), model=None, outcome="error")
        sent = run_health_check(store_conn, settings, outbox, at)
        assert bool([m for m in sent if "verification stage" in m]) is expected, at


# no job reaching the scored stage in 24 h

def test_no_job_scored_for_24h_alerts(store_conn, settings):
    _quiet_but_healthy(store_conn, NOW)
    with store_conn.cursor() as cur:
        cur.execute("UPDATE jobs SET stage_changed_at = %s WHERE stage = 'scored'",
                    (NOW - timedelta(hours=26),))
    store_conn.commit()
    issues = {i.key: i for i in evaluate_health(store_conn, settings, NOW)}
    assert "scoring_silent" in issues
    assert "no job has reached the scored stage in 26h00m" in issues["scoring_silent"].message


def test_a_job_scored_23h_ago_is_not_an_issue(store_conn, settings):
    _quiet_but_healthy(store_conn, NOW)
    with store_conn.cursor() as cur:
        cur.execute("UPDATE jobs SET stage_changed_at = %s WHERE stage = 'scored'",
                    (NOW - timedelta(hours=23),))
    store_conn.commit()
    assert {i.key for i in evaluate_health(store_conn, settings, NOW)} == set()


def test_a_delivered_job_counts_as_having_reached_scored(store_conn, settings):
    # Scored jobs move on to delivered at 08:00; the window must not empty out then.
    _quiet_but_healthy(store_conn, NOW)
    with store_conn.cursor() as cur:
        cur.execute("UPDATE jobs SET stage = 'delivered', stage_changed_at = %s WHERE stage = 'scored'",
                    (NOW - timedelta(hours=2),))
    store_conn.commit()
    assert {i.key for i in evaluate_health(store_conn, settings, NOW)} == set()


def test_an_aged_out_job_is_not_a_scored_job(store_conn, settings):
    # Tier 3/4 age-out moves `seen` jobs to `expired` without scoring them; a fresh
    # expiry must not make a dead scoring stage look alive.
    _quiet_but_healthy(store_conn, NOW)
    with store_conn.cursor() as cur:
        cur.execute("UPDATE jobs SET stage_changed_at = %s WHERE stage = 'scored'",
                    (NOW - timedelta(hours=26),))
    store_conn.commit()
    _reached_scored(store_conn, NOW - timedelta(minutes=1), stage="expired")
    assert "scoring_silent" in {i.key for i in evaluate_health(store_conn, settings, NOW)}


def test_a_store_that_never_scored_a_job_does_not_alert(store_conn, settings):
    _quiet_but_healthy(store_conn, NOW)
    with store_conn.cursor() as cur:
        cur.execute("DELETE FROM jobs")
    store_conn.commit()
    assert "scoring_silent" not in {i.key for i in evaluate_health(store_conn, settings, NOW)}


def test_the_scoring_alert_is_sent_at_most_once_a_day(store_conn, settings):
    _quiet_but_healthy(store_conn, NOW)
    with store_conn.cursor() as cur:
        cur.execute("UPDATE jobs SET stage_changed_at = %s WHERE stage = 'scored'",
                    (NOW - timedelta(hours=25),))
    store_conn.commit()
    outbox = Outbox()
    for hours, expected in ((0, True), (6, False), (23, False), (24, True)):
        at = NOW + timedelta(hours=hours)
        _refresh(store_conn, at)
        sent = run_health_check(store_conn, settings, outbox, at)
        assert bool([m for m in sent if "scored stage" in m]) is expected, at
