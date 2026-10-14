from datetime import datetime, timedelta, timezone

from src.jobstore import budget, heartbeat

NOW = datetime(2026, 10, 14, 23, 59, tzinfo=timezone.utc)


def test_usage_accumulates_within_a_utc_day(store_conn):
    budget.add_usage(store_conn, "openrouter", requests=1, input_tokens=100, output_tokens=20, now=NOW)
    budget.add_usage(store_conn, "openrouter", requests=2, input_tokens=50, now=NOW)
    usage = budget.used_today(store_conn, "openrouter", now=NOW)
    assert usage == budget.Usage(requests=3, input_tokens=150, output_tokens=20)


def test_usage_resets_at_the_utc_day_boundary(store_conn):
    budget.add_usage(store_conn, "openrouter", now=NOW)
    next_day = NOW + timedelta(minutes=2)
    assert budget.used_today(store_conn, "openrouter", now=next_day) == budget.Usage()


def test_usage_is_per_provider(store_conn):
    budget.add_usage(store_conn, "openrouter", now=NOW)
    assert budget.used_today(store_conn, "groq", now=NOW).requests == 0


def test_none_token_counts_are_treated_as_zero(store_conn):
    budget.add_usage(store_conn, "groq", requests=1, input_tokens=None, output_tokens=None, now=NOW)
    assert budget.used_today(store_conn, "groq", now=NOW) == budget.Usage(requests=1)


def test_heartbeat_start_then_beat_updates_state_and_detail(store_conn):
    heartbeat.start(store_conn, "process", now=NOW)
    heartbeat.beat(store_conn, "process", "running", {"waiting": 7}, now=NOW + timedelta(minutes=1))
    (row,) = heartbeat.read_all(store_conn)
    assert row.service == "process"
    assert row.started_at == NOW
    assert row.beat_at == NOW + timedelta(minutes=1)
    assert row.state == "running" and row.detail == {"waiting": 7}


def test_restart_resets_started_at(store_conn):
    heartbeat.start(store_conn, "process", now=NOW)
    heartbeat.start(store_conn, "process", now=NOW + timedelta(hours=2))
    (row,) = heartbeat.read_all(store_conn)
    assert row.started_at == NOW + timedelta(hours=2) and row.state == "starting"


def test_stage_requests_are_counted_per_provider_stage_and_utc_day(store_conn):
    def call(stage, provider, started):
        with store_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO llm_calls (id, stage, provider, request_model, latency_ms, batch_size, attempt, "
                "outcome, prompt_version, started_at) VALUES (gen_random_uuid(), %s, %s, 'm', 1, 1, 1, 'ok', "
                "'v', %s)", (stage, provider, started))

    call("verification", "openrouter", NOW)
    call("verification", "openrouter", NOW)
    call("scoring", "openrouter", NOW)
    call("verification", "groq", NOW)
    call("verification", "openrouter", NOW + timedelta(minutes=5))  # the next UTC day
    assert budget.stage_requests_today(store_conn, "openrouter", "verification", NOW) == 2
    assert budget.stage_requests_today(store_conn, "openrouter", "scoring", NOW) == 1
    assert budget.stage_requests_today(store_conn, "groq", "verification", NOW) == 1
