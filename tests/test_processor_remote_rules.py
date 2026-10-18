"""The rule pre-pass inside the remote stage: what it settles, and how it stores it."""
from datetime import datetime, timedelta, timezone

import pytest

from src.jobstore.sightings import CardInput, record_sighting
from src.processor.cheap import check_one
from src.processor.config import load_processor_config
from src.processor.remote import apply_deterministic_rules

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
FILLER = ("We build retrieval augmented generation pipelines with Python, PyTorch and FastAPI, "
          "and we ship them to production with Docker and GitHub Actions CI/CD. ") * 2


@pytest.fixture(scope="module")
def cfg():
    return load_processor_config()


def _described(conn, cfg, n, description, *, location="Milan, Italy", tier_hint=1, **overrides):
    values = dict(source="linkedin", source_job_id=f"r{n}",
                  source_url=f"https://www.linkedin.com/jobs/view/r{n}",
                  title=f"AI Engineer {n}", company=f"Company {n}", location=location,
                  tier_hint=tier_hint, description=description, description_source="linkedin")
    values.update(overrides)
    job_id = record_sighting(conn, CardInput(**values), NOW).job_id
    check_one(conn, job_id, cfg, NOW)
    conn.commit()
    return job_id


def _row(conn, job_id):
    with conn.cursor() as cur:
        cur.execute("SELECT stage, stage_reason, remote_verdict, remote_reason, remote_advice "
                    "FROM jobs WHERE id = %s", (job_id,))
        return cur.fetchone()


def test_a_clear_hybrid_phrase_is_rejected_by_rule(store_conn, cfg):
    job = _described(store_conn, cfg, 1, FILLER + "This is a hybrid role: three days a week in the office.")
    assert apply_deterministic_rules(store_conn, cfg, NOW) == 1
    stage, reason, verdict, stored_reason, _advice = _row(store_conn, job)
    assert (stage, reason, verdict) == ("filtered_out", "remote_rejected_rule", "rejected")
    assert stored_reason.startswith("rule: ")


def test_a_clear_remote_phrase_confirms_in_tier_one(store_conn, cfg):
    job = _described(store_conn, cfg, 1, FILLER + "This role is fully remote.")
    assert apply_deterministic_rules(store_conn, cfg, NOW) == 1
    stage, _reason, verdict, _stored, advice = _row(store_conn, job)
    assert (stage, verdict, advice) == ("described", "confirmed", "")


def test_a_remote_job_without_italy_eligibility_is_advised_in_tier_three(store_conn, cfg):
    job = _described(store_conn, cfg, 1, FILLER + "This role is fully remote.",
                     location="Berlin, Germany", tier_hint=3)
    assert apply_deterministic_rules(store_conn, cfg, NOW) == 1
    stage, _reason, verdict, _stored, advice = _row(store_conn, job)
    assert (stage, verdict) == ("described", "advised")
    assert advice == "remote, based in Germany - check eligibility"


def test_a_silent_description_becomes_not_stated_without_a_model_call(store_conn, cfg):
    job = _described(store_conn, cfg, 1, FILLER)
    assert apply_deterministic_rules(store_conn, cfg, NOW) == 1
    stage, _reason, verdict, stored_reason, advice = _row(store_conn, job)
    assert (stage, verdict, advice) == ("described", "not_stated", "")
    assert "no work-mode phrase" in stored_reason


def test_a_borderline_description_is_left_for_the_model(store_conn, cfg):
    job = _described(store_conn, cfg, 1, FILLER + "We offer smart working and flexible hours.")
    assert apply_deterministic_rules(store_conn, cfg, NOW) == 0
    assert _row(store_conn, job)[2] is None


def test_the_source_hint_settles_hybrid_before_any_text_rule(store_conn, cfg):
    job = _described(store_conn, cfg, 1, FILLER, remote_hint="hybrid")
    assert apply_deterministic_rules(store_conn, cfg, NOW) == 1
    assert _row(store_conn, job)[:3] == ("filtered_out", "remote_rejected_structured", "rejected")


def test_a_rejected_job_is_never_retried_by_the_batch_stage(store_conn, cfg):
    from src.processor.llm_stages import LlmKeys
    from src.processor.remote import run_remote_batches

    job = _described(store_conn, cfg, 2, FILLER + "This is a fully on-site role in Milan.")
    apply_deterministic_rules(store_conn, cfg, NOW)
    called = []

    def verify(**kwargs):
        called.append(kwargs)
        return kwargs["offers"], {"degraded": False}

    later = NOW + timedelta(hours=2)
    assert run_remote_batches(store_conn, cfg, later, LlmKeys("l", "g"), verify=verify) == 0
    assert called == []
    assert _row(store_conn, job)[2] == "rejected"
