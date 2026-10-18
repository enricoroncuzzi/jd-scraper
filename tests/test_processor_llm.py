from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.jobstore.sightings import CardInput, record_sighting
from src.models import ScoredOffer
from src.processor.cheap import check_one
from src.processor.config import load_processor_config
from src.processor.llm_stages import LlmKeys, due_batch
from src.processor.remote import apply_deterministic_rules, run_remote_batches, structured_verdict
from src.processor.scoring import run_scoring_batches
from src.remote_verifier import _DEGRADED_REASON
from tests.test_processor_cheap import ENGLISH

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
# A work-mode mention that the deterministic rules deliberately refuse to settle
# (remote as a perk, no confident phrase), so these tests exercise the model path
# rather than the rule pre-pass.
_UNDECIDED = ENGLISH + " We are a remote-friendly company with flexible working."
KEYS = LlmKeys("llm-key", "groq-key")


@pytest.fixture(scope="module")
def cfg():
    return load_processor_config()


def _described(conn, cfg, n, *, location="Milan, Italy", tier_hint=1, minutes_ago=0, **overrides):
    values = dict(source="freehire", source_job_id=f"j{n}", source_url=f"https://freehire.me/j{n}",
                  title=f"AI Engineer {n}", company=f"Company {n}", location=location,
                  tier_hint=tier_hint, description=_UNDECIDED, description_source="freehire")
    values.update(overrides)
    job_id = record_sighting(conn, CardInput(**values), NOW - timedelta(minutes=minutes_ago)).job_id
    check_one(conn, job_id, cfg, NOW - timedelta(minutes=minutes_ago))
    conn.commit()
    return job_id


def _row(conn, job_id):
    with conn.cursor() as cur:
        cur.execute("SELECT stage, stage_reason, remote_verdict, remote_reason, score, attempts "
                    "FROM jobs WHERE id = %s", (job_id,))
        return cur.fetchone()


class FakeVerify:
    """Stands in for verify_offers: answers every offer with `verdict`."""

    def __init__(self, verdict="confirmed", reason="because", degraded_ids=()):
        self.verdict, self.reason, self.degraded_ids = verdict, reason, set(degraded_ids)
        self.calls = []

    def __call__(self, offers, require_italy_eligibility, groq_api_key, llm_api_key,
                 groq_tokens_used_today, openrouter_requests_used_today, batch_size=None):
        self.calls.append({"batch_size": batch_size, "ids": [o.id for o in offers], "titles": [o.title for o in offers],
                           "italy": require_italy_eligibility, "groq": groq_api_key,
                           "llm": llm_api_key, "or_used": openrouter_requests_used_today})
        for offer in offers:
            degraded = offer.id in self.degraded_ids
            offer.remote_verdict = "unconfirmed" if degraded else self.verdict
            offer.remote_reason = _DEGRADED_REASON if degraded else self.reason
        return offers, {"degraded": False}


class FakeScore:
    def __init__(self, score=9, stop_after=None):
        self.score, self.stop_after, self.calls = score, stop_after, []

    def __call__(self, offers, profile, priority_keywords, exclude_keywords, llm_api_key,
                 batch_size=None):
        self.calls.append({"batch_size": batch_size, "titles": [o.title for o in offers], "profile": profile,
                           "priority": priority_keywords, "exclude": exclude_keywords})
        reached = offers if self.stop_after is None else offers[:self.stop_after]
        return [ScoredOffer(**o.model_dump(), score=self.score, comment="fits", summary="a role")
                for o in reached], {}


def test_due_batch_waits_for_size_or_age(cfg):
    from src.processor.state import JobRow
    def row(minutes):
        return JobRow(*(["x"] * 17), NOW - timedelta(minutes=minutes), None)
    rows = [row(1)] * 3
    assert due_batch(rows, 8, 30, NOW) == []
    assert due_batch([row(31)] + rows, 8, 30, NOW) == [row(31)] + rows
    assert len(due_batch([row(1)] * 10, 8, 30, NOW)) == 8


def test_tier_two_needs_no_remote_check(store_conn, cfg):
    job = _described(store_conn, cfg, 1, location="Zurich, Switzerland", tier_hint=2)
    assert apply_deterministic_rules(store_conn, cfg, NOW) == 1
    assert _row(store_conn, job)[2:4] == ("not_checked", "")


def test_only_the_hybrid_onsite_hint_is_a_shortcut():
    assert structured_verdict("onsite")[0] == "rejected"
    assert structured_verdict("hybrid")[0] == "rejected"
    # The confirm cases measured 87.3% and 66.7%: below the project's 95% bar.
    assert structured_verdict("remote_worldwide") is None
    assert structured_verdict("remote") is None
    assert structured_verdict(None) is None


def test_the_hybrid_onsite_hint_settles_without_a_model_call(store_conn, cfg):
    job = _described(store_conn, cfg, 1, remote_hint="onsite")
    assert apply_deterministic_rules(store_conn, cfg, NOW) == 1
    assert _row(store_conn, job)[:4] == ("filtered_out", "remote_rejected_structured",
                                         "rejected", "Source states the work mode. (onsite)")


def test_the_hint_shortcut_can_be_switched_off(store_conn, cfg):
    _described(store_conn, cfg, 1, remote_hint="onsite")
    off = replace(cfg, structured_remote_enabled=False)
    assert apply_deterministic_rules(store_conn, off, NOW) == 0


def test_a_full_batch_is_verified_with_the_tiers_italy_rule_and_stored(store_conn, cfg):
    jobs = [_described(store_conn, cfg, n) for n in range(8)]
    verify = FakeVerify("confirmed", "fully remote")
    assert run_remote_batches(store_conn, cfg, NOW, KEYS, verify=verify) == 8
    assert verify.calls[0]["italy"] is False and len(verify.calls[0]["ids"]) == 8
    assert (verify.calls[0]["llm"], verify.calls[0]["groq"]) == ("llm-key", "groq-key")
    assert _row(store_conn, jobs[0])[:4] == ("described", "", "confirmed", "fully remote")


def test_a_short_batch_waits_until_the_oldest_job_has_waited_thirty_minutes(store_conn, cfg):
    _described(store_conn, cfg, 1)
    verify = FakeVerify()
    assert run_remote_batches(store_conn, cfg, NOW, KEYS, verify=verify) == 0 and verify.calls == []
    assert run_remote_batches(store_conn, cfg, NOW + timedelta(minutes=31), KEYS, verify=verify) == 1


def test_a_rejected_job_leaves_the_pipeline_before_it_costs_a_scoring_call(store_conn, cfg):
    job = _described(store_conn, cfg, 1, minutes_ago=40)
    run_remote_batches(store_conn, cfg, NOW, KEYS, verify=FakeVerify("rejected", "needs office days"))
    assert _row(store_conn, job)[:4] == ("filtered_out", "remote_rejected", "rejected", "needs office days")


def test_a_verdict_that_is_only_a_stage_failure_is_not_stored_and_retries_later(store_conn, cfg):
    job = _described(store_conn, cfg, 1, minutes_ago=40)
    assert run_remote_batches(store_conn, cfg, NOW, KEYS, verify=FakeVerify(degraded_ids=[0])) == 0
    stage, _r, verdict, _reason, _s, attempts = _row(store_conn, job)
    assert (stage, verdict, attempts) == ("described", None, 1)
    with store_conn.cursor() as cur:
        cur.execute("SELECT next_attempt_at FROM jobs WHERE id = %s", (job,))
        assert cur.fetchone()[0] == NOW + timedelta(minutes=5)


def test_tier_one_and_the_italy_rule_tiers_are_never_mixed_in_a_batch(store_conn, cfg):
    _described(store_conn, cfg, 1, minutes_ago=40)
    _described(store_conn, cfg, 2, minutes_ago=40, location="Berlin, Germany", tier_hint=3)
    verify = FakeVerify()
    run_remote_batches(store_conn, cfg, NOW, KEYS, verify=verify)
    assert [call["italy"] for call in verify.calls] == [False, True]
    assert [call["titles"] for call in verify.calls] == [["AI Engineer 1"], ["AI Engineer 2"]]


def test_the_verifier_is_given_its_own_stage_count_not_the_whole_days_requests(store_conn, cfg):
    from src.jobstore import budget
    for stage, count in (("verification", 3), ("scoring", 50)):
        for _ in range(count):
            with store_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO llm_calls (id, stage, provider, request_model, latency_ms, batch_size, "
                    "attempt, outcome, prompt_version, started_at) VALUES (gen_random_uuid(), %s, "
                    "'openrouter', 'm', 1, 1, 1, 'ok', 'v', %s)", (stage, NOW))
        budget.add_usage(store_conn, "openrouter", requests=count, now=NOW)
    _described(store_conn, cfg, 1, minutes_ago=40)
    verify = FakeVerify()
    run_remote_batches(store_conn, cfg, NOW, KEYS, verify=verify)
    assert verify.calls[0]["or_used"] == 3


def test_no_llm_stage_runs_once_the_daily_stop_line_is_reached(store_conn, cfg):
    from src.jobstore import budget
    budget.add_usage(store_conn, "openrouter", requests=cfg.llm_stop_at_requests, now=NOW)
    _described(store_conn, cfg, 1, minutes_ago=40)
    verify, score = FakeVerify(), FakeScore()
    assert run_remote_batches(store_conn, cfg, NOW, KEYS, verify=verify) == 0
    assert run_scoring_batches(store_conn, cfg, NOW, KEYS, score=score) == 0
    assert verify.calls == [] and score.calls == []


def _verified(conn, cfg, n, **kwargs):
    job = _described(conn, cfg, n, **kwargs)
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET remote_verdict = 'confirmed' WHERE id = %s", (job,))
    conn.commit()
    return job


def test_a_full_scoring_batch_stores_score_comment_and_summary(store_conn, cfg):
    jobs = [_verified(store_conn, cfg, n) for n in range(5)]
    score = FakeScore(9)
    assert run_scoring_batches(store_conn, cfg, NOW, KEYS, score=score) == 5
    assert _row(store_conn, jobs[0])[:2] == ("scored", "") and _row(store_conn, jobs[0])[4] == 9
    assert score.calls[0]["profile"] == cfg.policies[1].candidate_profile


def test_unverified_and_rejected_jobs_are_not_scored(store_conn, cfg):
    _described(store_conn, cfg, 1, minutes_ago=40)  # no verdict yet
    score = FakeScore()
    assert run_scoring_batches(store_conn, cfg, NOW, KEYS, score=score) == 0 and score.calls == []


def test_tier_two_is_scored_with_its_own_profile_and_never_mixed_with_the_others(store_conn, cfg):
    _verified(store_conn, cfg, 1, minutes_ago=40)
    _verified(store_conn, cfg, 2, minutes_ago=40, location="Zurich, Switzerland", tier_hint=2)
    score = FakeScore()
    run_scoring_batches(store_conn, cfg, NOW, KEYS, score=score)
    profiles = {call["profile"] for call in score.calls}
    assert profiles == {cfg.policies[1].candidate_profile, cfg.policies[2].candidate_profile}
    assert all(len(call["titles"]) == 1 for call in score.calls)


def test_what_the_scorer_did_not_reach_waits_and_the_batch_loop_stops(store_conn, cfg):
    jobs = [_verified(store_conn, cfg, n) for n in range(5)]
    assert run_scoring_batches(store_conn, cfg, NOW, KEYS, score=FakeScore(stop_after=2)) == 2
    assert [_row(store_conn, j)[0] for j in jobs] == ["scored", "scored", "described", "described", "described"]
    assert _row(store_conn, jobs[4])[5] == 1


def test_the_configured_batch_sizes_are_passed_to_the_existing_stages(store_conn, cfg):
    for n in range(8):
        _verified(store_conn, cfg, n, minutes_ago=40)
    score = FakeScore()
    run_scoring_batches(store_conn, cfg, NOW, KEYS, score=score)
    assert score.calls[0]["batch_size"] == cfg.scoring_batch_size == 5
    for n in range(10, 18):
        _described(store_conn, cfg, n, minutes_ago=40)
    verify = FakeVerify()
    run_remote_batches(store_conn, cfg, NOW, KEYS, verify=verify)
    assert verify.calls[0]["batch_size"] == cfg.remote_check_batch_size == 8
