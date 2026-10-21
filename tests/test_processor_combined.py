from datetime import datetime, timezone

import openai
import pytest

from src.processor.combined import (
    CombinedItem, CombinedOutput, invoke_combined, offer_text, run_combined_batches,
)
from src.processor.config import load_processor_config
from tests.test_processor_llm import KEYS, _described, _row
from src.models import JobOffer

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def cfg():
    return load_processor_config()


class FakeChain:
    def __init__(self, *outcomes):
        self.outcomes, self.payloads = list(outcomes), []

    def invoke(self, payload, config=None):
        self.payloads.append(payload)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _output(count, verdict="confirmed", score=9, skip=()):
    return CombinedOutput(offers=[
        CombinedItem(id=i, verdict=verdict, reason="r", score=score, comment="c", summary="s")
        for i in range(count) if i not in skip])


def test_the_prompt_carries_the_verifier_rule_for_the_tier(store_conn, cfg):
    chain = FakeChain(_output(1))
    offers = [JobOffer(id=0, title="AI", company="Acme", link="x", description="d", description_status="ok")]
    invoke_combined(chain, offers, require_italy_eligibility=True, profile="p", priority_keywords=["LLM"],
                    exclude_keywords=["Java"], sleep=lambda _s: None)
    assert "living in Italy" in chain.payloads[0]["remote_rule"]
    other = FakeChain(_output(1))
    invoke_combined(other, offers, require_italy_eligibility=False, profile="p",
                    priority_keywords=[], exclude_keywords=[], sleep=lambda _s: None)
    assert "work mode only" in other.payloads[0]["remote_rule"]


def test_late_work_location_text_is_appended_when_it_falls_past_the_scoring_prefix():
    late = "x " * 4000 + " This role is based onsite in our Milan office."
    text = offer_text(JobOffer(id=0, title="T", company="C", link="l", description=late, description_status="ok"))
    assert "based onsite in our Milan office" in text and "later in the text" in text
    short = offer_text(JobOffer(id=0, title="T", company="C", link="l", description="Fully remote role.",
                                description_status="ok"))
    assert "later in the text" not in short


def test_upstream_errors_are_retried_with_a_growing_wait():
    sleeps = []
    ok = FakeChain(openai.InternalServerError("5xx", response=_resp(500), body=None), _output(1))
    offers = [JobOffer(id=0, title="T", company="C", link="l", description="d", description_status="ok")]
    items = invoke_combined(ok, offers, require_italy_eligibility=False, profile="p", priority_keywords=[],
                            exclude_keywords=[], sleep=sleeps.append)
    assert len(items) == 1 and sleeps == [10]


def test_daily_quota_exhaustion_is_raised_at_once_not_retried():
    quota = openai.RateLimitError("quota", response=_resp(429), body={"code": "token_quota_exceeded"})
    chain, sleeps = FakeChain(quota, _output(1)), []
    offers = [JobOffer(id=0, title="T", company="C", link="l", description="d", description_status="ok")]
    with pytest.raises(openai.RateLimitError):
        invoke_combined(chain, offers, require_italy_eligibility=False, profile="p", priority_keywords=[],
                        exclude_keywords=[], sleep=sleeps.append)
    assert sleeps == [] and len(chain.payloads) == 1


def _resp(status):
    import httpx
    return httpx.Response(status, request=httpx.Request("POST", "https://openrouter.ai"))


def test_one_call_stores_verdict_and_score_and_moves_the_job_on(store_conn, cfg):
    jobs = [_described(store_conn, cfg, n, minutes_ago=40) for n in range(2)]
    chain = FakeChain(_output(2, "confirmed", 9))
    assert run_combined_batches(store_conn, cfg, NOW, KEYS, chain=chain, sleep=lambda _s: None) == 2
    assert _row(store_conn, jobs[0])[:5] == ("scored", "", "confirmed", "r", 9)
    assert len(chain.payloads) == 1  # one request instead of a verify call plus a scoring call


def test_a_rejected_job_is_filtered_out_but_keeps_its_score_for_comparison(store_conn, cfg):
    job = _described(store_conn, cfg, 1, minutes_ago=40)
    run_combined_batches(store_conn, cfg, NOW, KEYS, chain=FakeChain(_output(1, "rejected", 4)),
                         sleep=lambda _s: None)
    assert _row(store_conn, job)[:5] == ("filtered_out", "remote_rejected", "rejected", "r", 4)


def test_items_the_model_skipped_wait_and_retry(store_conn, cfg):
    # Distinct ages keep the batch order deterministic: jobs.id is a random UUID, so
    # jobs with an identical first_seen_at would otherwise form the batch in any order.
    jobs = [_described(store_conn, cfg, n, minutes_ago=40 - n) for n in range(3)]
    done = run_combined_batches(store_conn, cfg, NOW, KEYS, chain=FakeChain(_output(3, skip=(1,))),
                                sleep=lambda _s: None)
    assert done == 2 and _row(store_conn, jobs[1])[0] == "described" and _row(store_conn, jobs[1])[5] == 1


def test_tier_two_is_left_to_the_scoring_stage(store_conn, cfg):
    _described(store_conn, cfg, 1, minutes_ago=40, location="Zurich, Switzerland", tier_hint=2)
    chain = FakeChain()
    assert run_combined_batches(store_conn, cfg, NOW, KEYS, chain=chain, sleep=lambda _s: None) == 0
    assert chain.payloads == []
