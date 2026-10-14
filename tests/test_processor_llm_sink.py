import uuid
from datetime import datetime, timezone

import pytest

from src import telemetry
from src.jobstore import budget
from src.processor.llm_sink import StoreLlmSink, batch_context

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def _call(**overrides):
    data = {"id": str(uuid.uuid4()), "run_uuid": None, "stage": "scoring", "provider": "openrouter",
            "request_model": "m", "response_model": "m", "input_tokens": 100, "output_tokens": 20,
            "latency_ms": 900, "batch_size": 5, "attempt": 1, "outcome": "ok", "error": None,
            "prompt_version": "abc", "started_at": NOW.isoformat()}
    data.update(overrides)
    return data


@pytest.fixture
def sink(store_conn, store_conn_factory):
    return StoreLlmSink(lambda: store_conn_factory(store_conn), "process")


def test_a_call_lands_in_llm_calls_and_the_day_budget(sink, store_conn):
    sink(_call())
    sink(_call(stage="verification", provider="groq", input_tokens=None))
    store_conn.commit()
    assert budget.used_today(store_conn, "openrouter", NOW) == budget.Usage(1, 100, 20)
    assert budget.used_today(store_conn, "groq", NOW) == budget.Usage(1, 0, 20)
    with store_conn.cursor() as cur:
        cur.execute("SELECT service, stage FROM llm_calls ORDER BY stage")
        assert cur.fetchall() == [("process", "scoring"), ("process", "verification")]


def test_a_repeated_call_id_is_counted_once(sink, store_conn):
    data = _call()
    sink(data)
    sink(data)
    store_conn.commit()
    with store_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM llm_calls")
        assert cur.fetchone()[0] == 1


def test_stage_requests_today_counts_one_stage_only(sink, store_conn):
    sink(_call())
    sink(_call())
    sink(_call(stage="verification"))
    store_conn.commit()
    assert budget.stage_requests_today(store_conn, "openrouter", "verification", NOW) == 1
    assert budget.stage_requests_today(store_conn, "openrouter", "scoring", NOW) == 2


def test_a_failing_database_never_raises_into_the_pipeline(capsys):
    broken = StoreLlmSink(lambda: (_ for _ in ()).throw(RuntimeError("db down")), "process")
    broken(_call())
    assert "could not record a call" in capsys.readouterr().out


def test_telemetry_routes_llm_calls_to_the_installed_sink():
    telemetry._reset_for_tests()
    received = []
    telemetry.set_llm_sink(received.append)
    with telemetry.llm_call(stage="scoring", provider="openrouter", request_model="m", batch_size=5,
                            attempt=1, prompt_version="v") as call:
        call.set_usage(response_model="m", input_tokens=10, output_tokens=2)
    telemetry._reset_for_tests()
    assert len(received) == 1 and received[0]["stage"] == "scoring" and received[0]["input_tokens"] == 10


def test_calls_made_inside_a_batch_context_are_linked_to_its_jobs(sink, store_conn):
    from src.jobstore.sightings import CardInput, record_sighting
    jobs = [record_sighting(store_conn, CardInput(source="linkedin", source_job_id=str(n),
                                                  source_url=f"https://l/{n}", title="t", company="c"), NOW).job_id
            for n in (1, 2)]
    store_conn.commit()
    data = _call()
    with batch_context(jobs):
        sink(data)
    sink(_call())  # outside any batch
    store_conn.commit()
    with store_conn.cursor() as cur:
        cur.execute("SELECT job_id FROM llm_call_jobs WHERE call_id = %s ORDER BY job_id", (data["id"],))
        assert sorted(row[0] for row in cur.fetchall()) == sorted(jobs)
        cur.execute("SELECT count(*) FROM llm_call_jobs")
        assert cur.fetchone()[0] == 2
