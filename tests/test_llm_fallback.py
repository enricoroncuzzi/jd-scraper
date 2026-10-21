"""The daily fallback-model guard: what answered, and when to say so.

Every test reads llm_calls rows from a throwaway store; nothing here reaches a
provider. The stage primaries come from the live pins, so these tests follow them.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src import llm_fallback
from src.llm_fallback import GuardConfig, format_usage_lines, load_guard_config, run_guard, stage_usages
from src.remote_verifier import _OPENROUTER_MODEL as PRIMARY
from src.scorer import _OPENROUTER_MODEL as SCORING_PRIMARY

ROME = ZoneInfo("Europe/Rome")
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
FALLBACK = "liquid/lfm-2.5-2.6b:free"
OTHER_FALLBACK = "dots-studio/dots-3-note-preview:free"
CFG = GuardConfig(enabled=True, share=0.20, min_calls=5)


class Outbox:
    def __init__(self, ok=True):
        self.messages, self.ok = [], ok

    def __call__(self, text):
        self.messages.append(text)
        return self.ok


def call(conn, stage, model, at=NOW, provider="openrouter", outcome="ok"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO llm_calls (id, stage, provider, request_model, response_model, latency_ms, "
            "batch_size, attempt, outcome, prompt_version, started_at) "
            "VALUES (gen_random_uuid(), %s, %s, %s, %s, 1000, 1, 1, %s, 'v', %s)",
            (stage, provider, PRIMARY, model, outcome, at))
    conn.commit()


def usage(conn, stage, now=NOW):
    return next(u for u in stage_usages(conn, now, ROME) if u.stage == stage)


def test_stage_usage_counts_primary_and_fallback_answers(store_conn):
    for _ in range(3):
        call(store_conn, "scoring", PRIMARY)
    for _ in range(2):
        call(store_conn, "scoring", FALLBACK)
    call(store_conn, "scoring", None, outcome="timeout")
    answered = usage(store_conn, "scoring")
    assert answered.calls == 6
    assert answered.unanswered == 1
    assert answered.primary_calls == 3
    assert answered.answered == 5
    assert answered.fallback_calls == 2
    assert answered.primary_share == pytest.approx(0.6)
    assert answered.top_model == (PRIMARY, 3)
    assert answered.fallbacks == ((FALLBACK, 2),)


def test_usage_covers_every_guarded_stage_and_ignores_other_stages(store_conn):
    call(store_conn, "combined", FALLBACK)
    assert [u.stage for u in stage_usages(store_conn, NOW, ROME)] == ["scoring", "verification", "tailoring"]
    assert all(u.calls == 0 for u in stage_usages(store_conn, NOW, ROME))


def test_only_openrouter_calls_of_that_stage_and_rome_day_count(store_conn):
    # 2026-10-13 21:59 UTC is 23:59 Rome on the 13th; 22:01 UTC is 00:01 Rome on the 14th.
    call(store_conn, "verification", FALLBACK, NOW - timedelta(hours=14, minutes=1))
    call(store_conn, "verification", FALLBACK, NOW - timedelta(hours=13, minutes=59))
    call(store_conn, "verification", FALLBACK, provider="groq")
    call(store_conn, "scoring", FALLBACK)
    stats = usage(store_conn, "verification")
    assert stats.calls == 1
    assert usage(store_conn, "scoring").calls == 1


def test_the_guard_alert_names_the_stage_models_primary_and_counts(store_conn):
    for _ in range(9):
        call(store_conn, "scoring", SCORING_PRIMARY)
    for _ in range(2):
        call(store_conn, "scoring", FALLBACK)
    call(store_conn, "scoring", OTHER_FALLBACK)
    outbox = Outbox()
    sent = run_guard(store_conn, CFG, outbox, NOW, ROME)
    assert sent == outbox.messages and len(sent) == 1
    text = sent[0]
    assert "scoring" in text
    assert FALLBACK in text and OTHER_FALLBACK in text
    assert SCORING_PRIMARY in text
    assert "3 of 12" in text and "25%" in text


def test_one_alert_per_stage_per_rome_day(store_conn):
    for _ in range(10):
        call(store_conn, "scoring", FALLBACK)
    outbox = Outbox()
    assert len(run_guard(store_conn, CFG, outbox, NOW, ROME)) == 1
    assert len(run_guard(store_conn, CFG, outbox, NOW + timedelta(hours=3), ROME)) == 0
    assert len(outbox.messages) == 1


def test_a_new_rome_day_alerts_again(store_conn):
    for _ in range(10):
        call(store_conn, "scoring", FALLBACK)
    outbox = Outbox()
    run_guard(store_conn, CFG, outbox, NOW, ROME)
    tomorrow = NOW + timedelta(hours=12)
    for _ in range(10):
        call(store_conn, "scoring", FALLBACK, tomorrow)
    assert len(run_guard(store_conn, CFG, outbox, tomorrow, ROME)) == 1
    assert len(outbox.messages) == 2


def test_each_stage_alerts_on_its_own(store_conn):
    for stage in ("scoring", "tailoring"):
        for _ in range(6):
            call(store_conn, stage, FALLBACK)
    outbox = Outbox()
    sent = run_guard(store_conn, CFG, outbox, NOW, ROME)
    assert len(sent) == 2
    assert any("scoring" in text for text in sent) and any("tailoring" in text for text in sent)


def test_a_stage_below_the_share_stays_quiet(store_conn):
    for _ in range(8):
        call(store_conn, "scoring", PRIMARY)
    for _ in range(2):
        call(store_conn, "scoring", FALLBACK)  # exactly 20%, not above
    assert run_guard(store_conn, CFG, Outbox(), NOW, ROME) == []


def test_a_stage_below_the_call_floor_stays_quiet(store_conn):
    for _ in range(4):
        call(store_conn, "scoring", FALLBACK)
    assert run_guard(store_conn, CFG, Outbox(), NOW, ROME) == []


def test_unanswered_calls_alone_alert_nothing(store_conn):
    # A model that never answered is not a fallback model answering; the stage
    # error surfaces elsewhere. This guard speaks only about answering models.
    for _ in range(10):
        call(store_conn, "scoring", None, outcome="error")
    assert run_guard(store_conn, CFG, Outbox(), NOW, ROME) == []


def test_the_guard_can_be_switched_off(store_conn):
    for _ in range(10):
        call(store_conn, "scoring", FALLBACK)
    assert run_guard(store_conn, GuardConfig(enabled=False), Outbox(), NOW, ROME) == []


def test_a_failed_send_is_not_remembered_and_is_retried(store_conn):
    for _ in range(10):
        call(store_conn, "scoring", FALLBACK)
    assert run_guard(store_conn, CFG, Outbox(ok=False), NOW, ROME) == []
    retry = Outbox()
    assert len(run_guard(store_conn, CFG, retry, NOW + timedelta(minutes=10), ROME)) == 1


def test_the_shipped_config_is_on_and_matches_the_documented_thresholds():
    config = load_guard_config()
    assert config.enabled is True
    assert config.share == 0.20
    assert config.min_calls == 5


def test_a_malformed_or_missing_config_keeps_the_defaults(tmp_path):
    missing = load_guard_config(str(tmp_path / "absent.json"))
    assert (missing.enabled, missing.share, missing.min_calls) == (True, 0.20, 5)
    broken = tmp_path / "broken.json"
    broken.write_text("not json")
    assert load_guard_config(str(broken)) == missing
    for bad in ('{"share": 20}', '{"min_calls": 0}', '{"enabled": "yes"}', '{"share": "a lot"}'):
        path = tmp_path / "bad.json"
        path.write_text(bad)
        assert load_guard_config(str(path)) == missing


def test_status_lines_show_calls_primary_share_and_top_model(store_conn):
    for _ in range(3):
        call(store_conn, "scoring", SCORING_PRIMARY)
    for _ in range(2):
        call(store_conn, "scoring", FALLBACK)
    call(store_conn, "scoring", None, outcome="invalid_output")
    lines = format_usage_lines(stage_usages(store_conn, NOW, ROME), CFG)
    header = lines[0]
    assert "Rome" in header and "20%" in header and "5" in header
    scoring = next(line for line in lines if "scoring" in line)
    assert "6 calls" in scoring and "1 unanswered" in scoring
    assert "60% by the primary" in scoring and FALLBACK in scoring
    assert PRIMARY in scoring
    verification = next(line for line in lines if "verification" in line)
    assert "0 calls" in verification


def test_status_lines_say_the_guard_is_off_when_it_is_switched_off(store_conn):
    lines = format_usage_lines(stage_usages(store_conn, NOW, ROME), GuardConfig(enabled=False))
    assert "guard off" in lines[0]


def test_a_stage_with_nothing_answered_never_crosses_the_threshold(store_conn):
    # Only reachable with a floor below 1, which the config loader refuses, but
    # the share must never divide by zero: GuardConfig is public.
    call(store_conn, "scoring", None, outcome="error")
    assert run_guard(store_conn, GuardConfig(enabled=True, min_calls=0), Outbox(), NOW, ROME) == []
