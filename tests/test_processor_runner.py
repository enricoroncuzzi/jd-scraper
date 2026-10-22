from datetime import datetime, timedelta, timezone

import pytest

from src.jobstore.sightings import CardInput, record_sighting
from src.linkedin.pace import Gate
from src.processor.cheap import check_one
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


def test_a_closed_gate_skips_descriptions_but_keeps_the_other_stages(store_conn, cfg):
    waiting = record_sighting(store_conn, _linkedin_card("3900000001"), NOW).job_id
    record_sighting(store_conn, _linkedin_card("3900000009", location="Austin, Texas, United States"), NOW)
    store_conn.commit()
    gate = Gate(open=False, reason="closed_hours", requests_today=0, window="weekday")
    get = ScriptedGet(response(200, FRAGMENT))
    tick = process_once(store_conn, FakePace(gate=gate), cfg, KEYS, now=NOW, get=get, sleep=lambda _s: None,
                        verify=FakeVerify(), score=FakeScore())
    assert tick.detail["described"] == 0 and get.calls == []
    assert tick.detail["cheap"] >= 1  # cheap checks, verification and scoring still ran
    assert _stage(store_conn, waiting)[0] == "seen"


def test_a_tick_with_other_work_skips_a_far_away_linkedin_turn_but_an_idle_tick_fetches(store_conn, cfg):
    foreign = CardInput(source="linkedin", source_job_id="3900000009",
                        source_url="https://www.linkedin.com/jobs/view/job-3900000009",
                        title="AI Engineer", company="Austin Co", location="Austin, Texas, United States",
                        tier_hint=1)
    record_sighting(store_conn, foreign, NOW)  # settled by the cheap checks, no model call
    waiting = record_sighting(store_conn, _linkedin_card("3900000001"), NOW).job_id
    store_conn.commit()
    get = ScriptedGet(response(200, FRAGMENT))
    kwargs = dict(sleep=lambda _s: None, verify=FakeVerify(), score=FakeScore())
    busy = process_once(store_conn, FakePace(wait=600), cfg, KEYS, now=NOW, get=get, **kwargs)
    assert busy.did_work and busy.detail["cheap"] >= 1 and busy.detail["described"] == 0 and get.calls == []
    idle = process_once(store_conn, FakePace(wait=600), cfg, KEYS, now=NOW, get=get, **kwargs)
    assert idle.detail["described"] == 1 and len(get.calls) == 1
    assert _stage(store_conn, waiting)[0] == "described"


def test_a_processor_tick_reuses_one_session(monkeypatch, store_conn, cfg):
    from types import SimpleNamespace
    import src.processor.runner as runner_module
    made = []
    monkeypatch.setattr(runner_module, "new_session",
                        lambda: made.append(1) or SimpleNamespace(get=ScriptedGet()))
    tick = runner_module.make_tick(FakePace(), cfg, KEYS)
    tick(store_conn)
    tick(store_conn)
    assert len(made) == 1


def test_a_tick_classifies_a_waiting_backlog_even_with_the_gate_closed(store_conn, cfg):
    job = record_sighting(store_conn, _linkedin_card("3900000001", location="Italy"), NOW).job_id
    check_one(store_conn, job, cfg, NOW)          # tiers it and stores its class
    with store_conn.cursor() as cur:              # then the class is lost, as after a config change
        cur.execute("UPDATE jobs SET description_class = NULL WHERE id = %s", (job,))
    store_conn.commit()
    gate = Gate(open=False, reason="closed_hours", requests_today=0, window="weekday")
    get = ScriptedGet()
    tick = process_once(store_conn, FakePace(gate=gate), cfg, KEYS, now=NOW, get=get,
                        sleep=lambda _s: None, verify=FakeVerify(), score=FakeScore())
    assert tick.detail["classified"] == 1 and get.calls == []
    with store_conn.cursor() as cur:
        cur.execute("SELECT description_class FROM jobs WHERE id = %s", (job,))
        assert cur.fetchone()[0] == 0


# A LinkedIn job can be tiered while its description is still queued, then meet a
# freehire/Himalayas twin that carries the text inline. The merge adopts the
# description (description_status 'ok') but leaves the survivor `seen`, so the
# cheap-check selector must pick it up again. Regression for the stranded
# seen+ok rows in the scout report (option C).
CLOSED = Gate(open=False, reason="closed_hours", requests_today=0, window="weekday")
TWIN_TITLE = "Azure DevOps Engineer GenAI Platform"
TWIN_COMPANY = "Pro Informatik AG"


def _linkedin_twin_card(job_id="4479793992"):
    return CardInput(source="linkedin", source_job_id=job_id,
                     source_url=f"https://ch.linkedin.com/jobs/view/azure-devops-{job_id}",
                     title=TWIN_TITLE, company=TWIN_COMPANY,
                     location="Zurich, Zurich, Switzerland", tier_hint=2)


def _freehire_twin_card(slug="azure-devops-ai4qmdwi"):
    return CardInput(source="freehire", source_job_id=slug,
                     source_url=f"https://freehire.me/jobs/{slug}",
                     title=TWIN_TITLE, company=TWIN_COMPANY, location="Zurich, Switzerland",
                     country="switzerland", tier_hint=2, description=ENGLISH,
                     description_source="freehire", source_detail="eures")


def _himalayas_twin_card(slug="azure-devops-himalayas"):
    return CardInput(source="himalayas", source_job_id=slug,
                     source_url=f"https://himalayas.app/jobs/{slug}",
                     title=TWIN_TITLE, company=TWIN_COMPANY, location="Zurich, Switzerland",
                     country="switzerland", tier_hint=2, description=ENGLISH,
                     description_source="himalayas")


def _twin_row(conn, job_id):
    with conn.cursor() as cur:
        cur.execute("SELECT stage, description_status, description_source, tier FROM jobs WHERE id = %s",
                    (job_id,))
        return cur.fetchone()


def _twin_tick(conn, cfg, when, gate, get, verify, score):
    return process_once(conn, FakePace(gate=gate), cfg, KEYS, now=when, get=get,
                        sleep=lambda _s: None, verify=verify, score=score)


def _twin_drain(conn, cfg, start, get, verify, score, ticks=8):
    when = start
    for _ in range(ticks):
        _twin_tick(conn, cfg, when, None, get, verify, score)
        when += timedelta(minutes=31)
    return when


def _tier_a_linkedin_job_and_wait(conn, cfg, card):
    """Record the LinkedIn card, tier it with the description gate closed, and
    return its job id: the `seen`+`pending`+tier state the merge later strands."""
    linkedin = record_sighting(conn, card, NOW).job_id
    conn.commit()
    _twin_tick(conn, cfg, NOW, CLOSED, ScriptedGet(), FakeVerify(), FakeScore())
    assert _twin_row(conn, linkedin) == ("seen", "pending", None, 2)
    return linkedin


def test_a_freehire_twin_advances_a_linkedin_job_that_waited_for_its_description(store_conn, cfg):
    linkedin = _tier_a_linkedin_job_and_wait(store_conn, cfg, _linkedin_twin_card())
    get, verify, score = ScriptedGet(), FakeVerify(), FakeScore(9)
    record_sighting(store_conn, _freehire_twin_card(), NOW + timedelta(days=2))
    store_conn.commit()
    merged_at = NOW + timedelta(days=2, hours=1)
    _twin_tick(store_conn, cfg, merged_at, CLOSED, get, verify, score)
    assert _twin_row(store_conn, linkedin) == ("seen", "ok", "freehire", 2)

    _twin_tick(store_conn, cfg, merged_at + timedelta(minutes=1), None, get, verify, score)
    assert _twin_row(store_conn, linkedin)[:2] == ("described", "ok")
    _twin_drain(store_conn, cfg, merged_at + timedelta(minutes=32), get, verify, score)
    assert _twin_row(store_conn, linkedin)[:2] == ("scored", "ok")
    assert get.calls == []  # the description came from the twin, no LinkedIn request


def test_a_freehire_twin_advances_a_rate_limited_linkedin_job(store_conn, cfg):
    linkedin = _tier_a_linkedin_job_and_wait(store_conn, cfg, _linkedin_twin_card())
    with store_conn.cursor() as cur:
        cur.execute("UPDATE jobs SET description_status = 'rate_limited', first_refused_at = %s "
                    "WHERE id = %s", (NOW, linkedin))
    store_conn.commit()
    get, verify, score = ScriptedGet(), FakeVerify(), FakeScore(9)
    record_sighting(store_conn, _freehire_twin_card(), NOW + timedelta(days=2))
    store_conn.commit()
    merged_at = NOW + timedelta(days=2, hours=1)
    _twin_tick(store_conn, cfg, merged_at, CLOSED, get, verify, score)
    assert _twin_row(store_conn, linkedin) == ("seen", "ok", "freehire", 2)

    _twin_drain(store_conn, cfg, merged_at + timedelta(minutes=1), get, verify, score)
    assert _twin_row(store_conn, linkedin)[:2] == ("scored", "ok")
    assert get.calls == []


def test_a_himalayas_twin_advances_a_linkedin_job_that_waited_for_its_description(store_conn, cfg):
    linkedin = _tier_a_linkedin_job_and_wait(store_conn, cfg, _linkedin_twin_card())
    get, verify, score = ScriptedGet(), FakeVerify(), FakeScore(9)
    record_sighting(store_conn, _himalayas_twin_card(), NOW + timedelta(days=2))
    store_conn.commit()
    merged_at = NOW + timedelta(days=2, hours=1)
    _twin_tick(store_conn, cfg, merged_at, CLOSED, get, verify, score)
    assert _twin_row(store_conn, linkedin) == ("seen", "ok", "himalayas", 2)

    _twin_drain(store_conn, cfg, merged_at + timedelta(minutes=1), get, verify, score)
    assert _twin_row(store_conn, linkedin)[:2] == ("scored", "ok")
    assert get.calls == []
