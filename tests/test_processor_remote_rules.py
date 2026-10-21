"""The rule pre-pass inside the remote stage: what it settles, and how it stores it."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.jobstore.sightings import CardInput, record_sighting
from src.processor.cheap import check_one
from src.processor.config import load_processor_config
from src.processor.llm_stages import LlmKeys
from src.processor.remote import (
    SOURCE_HINT_REASON, apply_deterministic_rules, run_remote_batches, structured_verdict,
)

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


def test_a_plain_remote_job_in_tier_three_is_left_to_the_model(store_conn, cfg):
    job = _described(store_conn, cfg, 1, FILLER + "This role is fully remote.",
                     location="Berlin, Germany", tier_hint=3)
    assert apply_deterministic_rules(store_conn, cfg, NOW) == 0
    assert _row(store_conn, job)[2] is None


def test_an_explicit_foreign_scope_is_advised_by_rule_in_tier_three(store_conn, cfg):
    job = _described(store_conn, cfg, 1, FILLER + "This role is fully remote. Remote (UK).",
                     location="London, United Kingdom", tier_hint=3)
    assert apply_deterministic_rules(store_conn, cfg, NOW) == 1
    stage, _reason, verdict, _stored, advice = _row(store_conn, job)
    assert (stage, verdict) == ("described", "advised")
    assert advice == "remote, limited to the UK - check eligibility"


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


def test_a_silent_text_with_a_remote_hint_is_advised(store_conn, cfg):
    job = _described(store_conn, cfg, 6, FILLER, remote_hint="remote")
    assert apply_deterministic_rules(store_conn, cfg, NOW) == 1
    stage, _reason, verdict, stored, advice = _row(store_conn, job)
    assert (stage, verdict) == ("described", "advised")
    assert stored == SOURCE_HINT_REASON
    assert advice == "the source marks it remote"


def test_a_silent_text_with_a_worldwide_hint_in_tier_three_carries_the_eligibility_advice(store_conn, cfg):
    job = _described(store_conn, cfg, 7, FILLER, location="Berlin, Germany",
                     tier_hint=3, remote_hint="remote_worldwide")
    assert apply_deterministic_rules(store_conn, cfg, NOW) == 1
    assert _row(store_conn, job)[2:5] == ("advised", SOURCE_HINT_REASON,
                                          "the source marks it remote - check eligibility")


def test_a_hint_never_overrides_a_rule_that_already_decided(store_conn, cfg):
    job = _described(store_conn, cfg, 8, FILLER + "This role is fully remote. Remote (UK).",
                     location="London, United Kingdom", tier_hint=3, remote_hint="remote")
    assert apply_deterministic_rules(store_conn, cfg, NOW) == 1
    assert _row(store_conn, job)[2::2] == ("advised", "remote, limited to the UK - check eligibility")


def test_a_short_description_with_a_hint_still_goes_to_the_model(store_conn, cfg):
    job = _described(store_conn, cfg, 9, "Python and LLM agents.", remote_hint="remote")
    assert apply_deterministic_rules(store_conn, cfg, NOW) == 0
    assert _row(store_conn, job)[2] is None


def test_an_explicit_full_remote_title_is_not_rejected_by_a_hybrid_hint(store_conn, cfg):
    job = _described(store_conn, cfg, 11, FILLER, location="Madrid, Spain", tier_hint=3,
                     title="Junior AI Engineer - Pharma (Full remote)", remote_hint="hybrid")
    assert apply_deterministic_rules(store_conn, cfg, NOW) == 0
    assert _row(store_conn, job)[:3] == ("described", "", None)


def test_a_hybrid_hint_without_an_explicit_full_remote_still_rejects(store_conn, cfg):
    job = _described(store_conn, cfg, 12, FILLER, location="Madrid, Spain", tier_hint=3,
                     title="Junior AI Engineer - Pharma", remote_hint="hybrid")
    assert apply_deterministic_rules(store_conn, cfg, NOW) == 1
    assert _row(store_conn, job)[:3] == ("filtered_out", "remote_rejected_structured", "rejected")


def test_in_tier_one_a_source_text_conflict_goes_to_the_model_not_to_confirmed(store_conn, cfg):
    # A loose "fully remote" phrase anywhere in the text must not let the rule
    # layer settle a source-conflict as confirmed (confirmed is the only verdict
    # that gets auto-tailored); the model decides, as it does today.
    job = _described(store_conn, cfg, 13,
                     FILLER + "Our interview process is fully remote. This position is hybrid.",
                     remote_hint="hybrid")
    assert apply_deterministic_rules(store_conn, cfg, NOW) == 0
    assert _row(store_conn, job)[2] is None


def test_the_structured_override_can_be_switched_off(store_conn, cfg):
    job = _described(store_conn, cfg, 14, FILLER, title="AI Engineer (Full remote)", remote_hint="hybrid")
    assert apply_deterministic_rules(store_conn, replace(cfg, explicit_remote_wins=False), NOW) == 1
    assert _row(store_conn, job)[2] == "rejected"


def test_the_hint_advice_can_be_switched_off(store_conn, cfg):
    job = _described(store_conn, cfg, 10, FILLER, remote_hint="remote")
    assert apply_deterministic_rules(store_conn, replace(cfg, remote_hint_advice=False), NOW) == 1
    assert _row(store_conn, job)[2] == "not_stated"


def test_the_cloud_hybrid_guard_can_be_switched_off(store_conn, cfg):
    job = _described(store_conn, cfg, 4, FILLER + "We run secure hybrid environments for clients.")
    assert apply_deterministic_rules(store_conn, replace(cfg, cloud_hybrid_guard=False), NOW) == 1
    assert _row(store_conn, job)[:3] == ("filtered_out", "remote_rejected_rule", "rejected")


def test_an_occasional_presence_phrase_is_left_to_the_model(store_conn, cfg):
    job = _described(store_conn, cfg, 3, FILLER + "Occasional on-site presence at the office.")
    assert apply_deterministic_rules(store_conn, cfg, NOW) == 0
    assert _row(store_conn, job)[2] is None


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


JOB = ("Work on the data platform with Python and dbt. The team is distributed and works "
       "from home most of the year, with a team offsite once a year. ") * 2
MIXED_OFFICE_DAYS = ("Two days a week in the office (Tuesday and Thursday), plus a "
                     "quarterly team offsite.")


def _rejecting_verify(reason):
    def verify(**kwargs):
        for offer in kwargs["offers"]:
            offer.remote_verdict = "rejected"
            offer.remote_reason = reason
        return kwargs["offers"], {"degraded": False}
    return verify


def test_a_remote_hint_is_not_rejected_on_occasional_presence_alone(store_conn, cfg):
    job = _described(store_conn, cfg, 15, JOB, remote_hint="remote")
    later = NOW + timedelta(minutes=31)
    verify = _rejecting_verify("Onsite team events in Warsaw and global team offsites twice a year.")
    assert run_remote_batches(store_conn, cfg, later, LlmKeys("l", "g"), verify=verify) == 1
    stage, _reason, verdict, stored, advice = _row(store_conn, job)
    assert (stage, verdict) == ("described", "advised")
    assert stored == SOURCE_HINT_REASON
    assert advice == "the source marks it remote"


def test_a_remote_hint_with_the_office_days_and_an_offsite_in_one_sentence_still_rejects(store_conn, cfg):
    job = _described(store_conn, cfg, 16, JOB + MIXED_OFFICE_DAYS, remote_hint="remote")
    later = NOW + timedelta(minutes=31)
    verify = _rejecting_verify("Two days in the office plus team offsites")
    assert run_remote_batches(store_conn, cfg, later, LlmKeys("l", "g"), verify=verify) == 1
    assert _row(store_conn, job)[:3] == ("filtered_out", "remote_rejected", "rejected")


def test_a_remote_hint_with_a_separate_office_days_sentence_still_rejects(store_conn, cfg):
    job = _described(store_conn, cfg, 17,
                     JOB + "This is a hybrid role: three days a week in the office.",
                     remote_hint="remote")
    later = NOW + timedelta(minutes=31)
    verify = _rejecting_verify("Onsite team events in Warsaw.")
    assert run_remote_batches(store_conn, cfg, later, LlmKeys("l", "g"), verify=verify) == 1
    assert _row(store_conn, job)[:3] == ("filtered_out", "remote_rejected", "rejected")


def test_a_paraphrased_occasional_reason_is_still_recognised(store_conn, cfg):
    job = _described(store_conn, cfg, 18, JOB, remote_hint="remote_worldwide")
    later = NOW + timedelta(minutes=31)
    verify = _rejecting_verify("Requires a few trips a year to headquarters for team gatherings.")
    assert run_remote_batches(store_conn, cfg, later, LlmKeys("l", "g"), verify=verify) == 1
    assert _row(store_conn, job)[2] == "advised"


def test_a_sprint_reason_without_a_frequency_word_is_recognised(store_conn, cfg):
    job = _described(store_conn, cfg, 21, JOB, remote_hint="remote")
    later = NOW + timedelta(minutes=31)
    verify = _rejecting_verify("Mandatory attendance at quarterly team sprints in London")
    assert run_remote_batches(store_conn, cfg, later, LlmKeys("l", "g"), verify=verify) == 1
    assert _row(store_conn, job)[2] == "advised"


def test_a_quarterly_offsite_reason_stays_occasional(store_conn, cfg):
    # "once a quarter" is occasional presence, not a regular cadence.
    job = _described(store_conn, cfg, 23, JOB, remote_hint="remote")
    later = NOW + timedelta(minutes=31)
    verify = _rejecting_verify("Team offsites once a quarter in Berlin")
    assert run_remote_batches(store_conn, cfg, later, LlmKeys("l", "g"), verify=verify) == 1
    assert _row(store_conn, job)[2] == "advised"


@pytest.mark.parametrize("text,reason", [
    ("You come to the office on Tuesdays and Thursdays.",
     "Office presence on Tuesdays and Thursdays plus quarterly team events"),
    ("We expect you in the office twice a week.",
     "Twice a week in the office, plus team offsites"),
    ("In-person collaboration in Milan every Monday and Friday.",
     "Requires on-site presence every Monday and Friday and team events"),
])
def test_a_model_reason_naming_a_regular_cadence_is_not_occasional(store_conn, cfg, text, reason):
    job = _described(store_conn, cfg, 22, JOB + text, remote_hint="remote")
    later = NOW + timedelta(minutes=31)
    assert run_remote_batches(store_conn, cfg, later, LlmKeys("l", "g"),
                              verify=_rejecting_verify(reason)) == 1
    assert _row(store_conn, job)[:3] == ("filtered_out", "remote_rejected", "rejected")


def test_a_rejection_without_a_remote_hint_is_untouched(store_conn, cfg):
    job = _described(store_conn, cfg, 19, JOB)
    later = NOW + timedelta(minutes=31)
    verify = _rejecting_verify("Onsite team events in Warsaw twice a year.")
    assert run_remote_batches(store_conn, cfg, later, LlmKeys("l", "g"), verify=verify) == 1
    assert _row(store_conn, job)[2] == "rejected"


def test_the_occasional_presence_guard_on_model_answers_can_be_switched_off(store_conn, cfg):
    job = _described(store_conn, cfg, 20, JOB, remote_hint="remote")
    later = NOW + timedelta(minutes=31)
    verify = _rejecting_verify("Onsite team events in Warsaw twice a year.")
    off = replace(cfg, remote_hint_never_reject=False)
    assert run_remote_batches(store_conn, off, later, LlmKeys("l", "g"), verify=verify) == 1
    assert _row(store_conn, job)[2] == "rejected"


@pytest.mark.parametrize("text,reason", [
    ("Hybrid role. Two days weekly at our Milan site. Team sprints.",
     "2 days in office per week, plus team sprints"),
    ("Hybrid role. Two days weekly at our Milan site. Team sprints.",
     "2 days/week in the office and sprints"),
    ("Hybrid role. Two days weekly at our Milan site. Team sprints.",
     "Hybrid: 2 office days a week and sprints"),
    ("Hybrid role. Two days weekly at our Milan site. Team sprints.",
     "hybrid role with team sprints"),
    ("Hybrid position. You will be in our Milan office two days weekly. We run two-week sprints.",
     "Hybrid position with sprints in the office"),
    ("Remote-friendly. Team day in the Berlin office, two days in the week. Sprints of 2 weeks.",
     "Team day in office, two days in the week; sprints"),
])
def test_a_regular_hybrid_reason_is_never_overridden(store_conn, cfg, text, reason):
    # A reason that mentions an occasional word ("sprints", "team events") while
    # stating a regular cadence must keep the model's rejection.
    job = _described(store_conn, cfg, 24, JOB + text, remote_hint="remote")
    later = NOW + timedelta(minutes=31)
    assert run_remote_batches(store_conn, cfg, later, LlmKeys("l", "g"),
                              verify=_rejecting_verify(reason)) == 1
    assert _row(store_conn, job)[:3] == ("filtered_out", "remote_rejected", "rejected")
