from datetime import datetime, timedelta, timezone

import pytest

from src.jobstore.sightings import CardInput, record_sighting
from src.processor.config import load_processor_config
from src.processor.llm_stages import LlmKeys
from src.processor.runner import process_once, waiting_counts
from tests.linkedin_fakes import FakePace, ScriptedGet, response
from tests.test_processor_cheap import ENGLISH
from tests.test_processor_llm import FakeScore, FakeVerify

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
KEYS = LlmKeys("llm-key", "groq-key")
FRAGMENT = f'<div class="show-more-less-html__markup">{ENGLISH}</div>'


@pytest.fixture(scope="module")
def cfg():
    return load_processor_config()


def _linkedin_card(job_id, location="Milan, Italy", tier_hint=1):
    return CardInput(source="linkedin", source_job_id=job_id,
                     source_url=f"https://www.linkedin.com/jobs/view/job-{job_id}",
                     title=f"AI Engineer {job_id}", company=f"Company {job_id}", location=location,
                     tier_hint=tier_hint)


def _stage(conn, job_id):
    with conn.cursor() as cur:
        cur.execute("SELECT stage, stage_reason, remote_verdict, score FROM jobs WHERE id = %s", (job_id,))
        return cur.fetchone()


def test_a_linkedin_card_flows_all_the_way_to_scored(store_conn, cfg):
    job = record_sighting(store_conn, _linkedin_card("3900000001"), NOW).job_id
    store_conn.commit()
    get = ScriptedGet(response(200, FRAGMENT))
    verify, score = FakeVerify("confirmed", "remote ok"), FakeScore(9)
    kwargs = dict(get=get, sleep=lambda _s: None, verify=verify, score=score)

    first = process_once(store_conn, FakePace(), cfg, KEYS, now=NOW, **kwargs)
    assert first.did_work and _stage(store_conn, job) == ("described", "", None, None)
    assert first.detail["waiting"] == {"described": 1}

    later = NOW + timedelta(minutes=31)  # a lone job waits for a batch, then the flush time passes
    second = process_once(store_conn, FakePace(), cfg, KEYS, now=later, **kwargs)
    assert _stage(store_conn, job) == ("scored", "", "confirmed", 9)
    assert second.detail["checked"] == 1 and second.detail["scored"] == 1
    assert len(get.calls) == 1 and len(verify.calls) == 1 and len(score.calls) == 1


def test_an_idle_store_reports_no_work(store_conn, cfg):
    tick = process_once(store_conn, FakePace(), cfg, KEYS, now=NOW, get=ScriptedGet(),
                        sleep=lambda _s: None, verify=FakeVerify(), score=FakeScore())
    assert not tick.did_work and tick.detail["waiting"] == {}


def test_out_of_scope_and_foreign_language_jobs_never_reach_a_model(store_conn, cfg):
    record_sighting(store_conn, _linkedin_card("3900000001", location="Austin, Texas, United States"), NOW)
    store_conn.commit()
    verify, score = FakeVerify(), FakeScore()
    process_once(store_conn, FakePace(), cfg, KEYS, now=NOW + timedelta(hours=1), get=ScriptedGet(),
                 sleep=lambda _s: None, verify=verify, score=score)
    assert verify.calls == [] and score.calls == []
    assert waiting_counts(store_conn) == {}


def test_a_linkedin_refusal_leaves_the_job_waiting_and_costs_no_model_call(store_conn, cfg):
    job = record_sighting(store_conn, _linkedin_card("3900000001"), NOW).job_id
    store_conn.commit()
    verify = FakeVerify()
    tick = process_once(store_conn, FakePace(), cfg, KEYS, now=NOW, get=ScriptedGet(*[response(429)] * 4),
                        sleep=lambda _s: None, verify=verify, score=FakeScore())
    assert _stage(store_conn, job)[:2] == ("seen", "") and verify.calls == []
    assert tick.detail["waiting"] == {"seen": 1}


def test_the_merged_call_replaces_the_two_stages_when_switched_on(store_conn, cfg):
    from dataclasses import replace
    from tests.test_processor_combined import FakeChain, _output
    job = record_sighting(store_conn, _linkedin_card("3900000001"), NOW).job_id
    store_conn.commit()
    on = replace(cfg, merged_call_enabled=True)
    verify, score, chain = FakeVerify(), FakeScore(), FakeChain(_output(1, "confirmed", 8))
    process_once(store_conn, FakePace(), on, KEYS, now=NOW, get=ScriptedGet(response(200, FRAGMENT)),
                 sleep=lambda _s: None, verify=verify, score=score, chain=chain)
    process_once(store_conn, FakePace(), on, KEYS, now=NOW + timedelta(minutes=31), get=ScriptedGet(),
                 sleep=lambda _s: None, verify=verify, score=score, chain=chain)
    assert _stage(store_conn, job) == ("scored", "", "confirmed", 8)
    assert verify.calls == [] and score.calls == [] and len(chain.payloads) == 1
