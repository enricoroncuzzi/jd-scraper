import json
import math
import time
from unittest.mock import MagicMock

import groq
import openai
import pytest

from src.models import JobOffer
from src.remote_verifier import (
    BATCH_SIZE,
    GROQ_DAILY_TOKEN_LIMIT,
    OPENROUTER_VERIFICATION_DAILY_REQUEST_CAP,
    _DEGRADED_REASON,
    _GROQ_TPD_HEADROOM_TOKENS,
    _MAX_DESC_CHARS,
    _VerdictOutput,
    _extract_policy_excerpt,
    _is_daily_quota_exceeded,
    _verify_batch,
    verify_offers,
)


def _offer(offer_id, description="We are fully remote across the EU.", status="ok"):
    return JobOffer(
        id=offer_id, title="AI Engineer", company="Acme",
        location="Berlin, Germany", link=f"https://x/{offer_id}",
        description=description, description_status=status,
    )


def _mock_groq(monkeypatch, payloads):
    """payloads: list of dicts returned by successive completion calls."""
    calls = {"prompts": [], "count": 0}

    def create(**kwargs):
        calls["prompts"].append(kwargs["messages"][0]["content"])
        payload = payloads[min(calls["count"], len(payloads) - 1)]
        calls["count"] += 1
        if isinstance(payload, Exception):
            raise payload
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = json.dumps(payload)
        response.usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        return response

    client = MagicMock()
    client.chat.completions.create = create
    monkeypatch.setattr("src.remote_verifier._client", lambda key: client)
    return calls


def test_parses_all_three_verdicts(monkeypatch):
    _mock_groq(monkeypatch, [{"offers": [
        {"id": 1, "verdict": "confirmed", "reason": "States remote anywhere in the EU."},
        {"id": 2, "verdict": "rejected", "reason": "Requires two days per week on site."},
        {"id": 3, "verdict": "unconfirmed", "reason": "Says remote without naming a country."},
    ]}])

    verified, usage = verify_offers([_offer(1), _offer(2), _offer(3)], True, "key")

    by_id = {o.id: o for o in verified}
    assert by_id[1].remote_verdict == "confirmed"
    assert by_id[2].remote_verdict == "rejected"
    assert by_id[3].remote_verdict == "unconfirmed"
    assert by_id[2].remote_reason == "Requires two days per week on site."
    assert usage["total_tokens"] == 15


def test_empty_description_short_circuits_without_a_call(monkeypatch):
    calls = _mock_groq(monkeypatch, [{"offers": []}])

    verified, usage = verify_offers([_offer(1, description="", status="failed")], True, "key")

    assert calls["count"] == 0
    assert verified[0].remote_verdict == "unconfirmed"
    assert "description" in verified[0].remote_reason.lower()
    assert usage["total_tokens"] == 0


def test_failed_batch_falls_back_to_unconfirmed_not_rejected(monkeypatch):
    _mock_groq(monkeypatch, [RuntimeError("groq exploded")])
    monkeypatch.setattr("src.remote_verifier.time.sleep", lambda s: None)

    verified, _ = verify_offers([_offer(1), _offer(2)], True, "key")

    assert [o.remote_verdict for o in verified] == ["unconfirmed", "unconfirmed"]


def test_missing_api_key_degrades_the_whole_stage(monkeypatch):
    calls = _mock_groq(monkeypatch, [{"offers": []}])

    verified, usage = verify_offers([_offer(1), _offer(2)], True, "")

    assert calls["count"] == 0
    assert all(o.remote_verdict == "unconfirmed" for o in verified)
    assert usage["total_tokens"] == 0
    assert usage["degraded"] is True


def test_offer_missing_from_the_response_becomes_unconfirmed(monkeypatch):
    _mock_groq(monkeypatch, [{"offers": [
        {"id": 1, "verdict": "confirmed", "reason": "Remote anywhere in the EU."},
    ]}])

    verified, _ = verify_offers([_offer(1), _offer(2)], True, "key")

    by_id = {o.id: o for o in verified}
    assert by_id[1].remote_verdict == "confirmed"
    assert by_id[2].remote_verdict == "unconfirmed"


def test_italy_eligibility_flag_reaches_the_prompt(monkeypatch):
    calls = _mock_groq(monkeypatch, [{"offers": [
        {"id": 1, "verdict": "confirmed", "reason": "Remote anywhere in the EU."},
    ]}])

    verify_offers([_offer(1)], True, "key")
    assert "Italy" in calls["prompts"][0]

    calls["prompts"].clear()
    verify_offers([_offer(1)], False, "key")
    assert "Italy" not in calls["prompts"][0]


def test_returns_every_offer_in_input_order(monkeypatch):
    monkeypatch.setattr("src.remote_verifier.time.sleep", lambda s: None)
    _mock_groq(monkeypatch, [{"offers": [
        {"id": i, "verdict": "confirmed", "reason": "Remote."} for i in range(1, 13)
    ]}])

    offers = [_offer(i) for i in range(1, 13)]
    verified, _ = verify_offers(offers, True, "key")

    assert [o.id for o in verified] == list(range(1, 13))


def test_degraded_is_set_only_when_every_batch_fails(monkeypatch):
    _mock_groq(monkeypatch, [RuntimeError("groq exploded")])
    monkeypatch.setattr("src.remote_verifier.time.sleep", lambda s: None)
    _, usage = verify_offers([_offer(1)], True, "key")
    assert usage["degraded"] is True


def test_skipped_descriptions_alone_do_not_count_as_degraded(monkeypatch):
    _mock_groq(monkeypatch, [{"offers": []}])
    _, usage = verify_offers([_offer(1, description="", status="failed")], True, "key")
    assert usage["degraded"] is False


def test_empty_input_makes_no_call(monkeypatch):
    calls = _mock_groq(monkeypatch, [{"offers": []}])
    verified, usage = verify_offers([], True, "key")
    assert verified == []
    assert calls["count"] == 0
    assert usage["total_tokens"] == 0


def test_content_free_fallback_description_short_circuits_without_a_call(monkeypatch):
    calls = _mock_groq(monkeypatch, [{"offers": []}])

    # What src/scraper.py falls back to when a job page is unreadable.
    offer = _offer(1, description="AI Engineer at Acme", status="partial")
    verified, usage = verify_offers([offer], True, "key")

    assert calls["count"] == 0
    assert verified[0].remote_verdict == "unconfirmed"
    assert "description" in verified[0].remote_reason.lower()
    assert usage["total_tokens"] == 0


def test_a_real_partial_description_is_still_verified(monkeypatch):
    calls = _mock_groq(monkeypatch, [{"offers": [
        {"id": 1, "verdict": "rejected", "reason": "Requires three days on site."},
    ]}])

    offer = _offer(1, description="AI Engineer at Acme, three days a week on site.",
                   status="partial")
    verified, _ = verify_offers([offer], True, "key")

    assert calls["count"] == 1
    assert verified[0].remote_verdict == "rejected"


def test_client_construction_failure_degrades_instead_of_raising(monkeypatch):
    calls = _mock_groq(monkeypatch, [{"offers": []}])

    def boom(key):
        raise ImportError("No module named 'groq'")

    monkeypatch.setattr("src.remote_verifier._client", boom)

    verified, usage = verify_offers([_offer(1), _offer(2)], True, "key")

    assert calls["count"] == 0
    assert [o.remote_verdict for o in verified] == ["unconfirmed", "unconfirmed"]
    assert all(o.remote_reason == _DEGRADED_REASON for o in verified)
    assert usage["degraded"] is True
    assert usage["total_tokens"] == 0


def test_client_construction_failure_with_nothing_to_check_is_not_degraded(monkeypatch):
    def boom(key):
        raise ImportError("No module named 'groq'")

    monkeypatch.setattr("src.remote_verifier._client", boom)

    verified, usage = verify_offers([_offer(1, description="", status="failed")], True, "key")

    assert verified[0].remote_verdict == "unconfirmed"
    assert usage["degraded"] is False


def test_mixed_case_verdicts_are_normalized(monkeypatch):
    _mock_groq(monkeypatch, [{"offers": [
        {"id": 1, "verdict": "Confirmed", "reason": "States remote anywhere in the EU."},
        {"id": 2, "verdict": " REJECTED ", "reason": "Requires two days per week on site."},
        {"id": 3, "verdict": "Unconfirmed", "reason": "Says remote without naming a country."},
    ]}])

    verified, _ = verify_offers([_offer(1), _offer(2), _offer(3)], True, "key")

    by_id = {o.id: o for o in verified}
    assert by_id[1].remote_verdict == "confirmed"
    assert by_id[2].remote_verdict == "rejected"
    assert by_id[3].remote_verdict == "unconfirmed"
    assert by_id[1].remote_reason == "States remote anywhere in the EU."


def test_an_unknown_verdict_word_still_falls_back_to_unconfirmed(monkeypatch):
    _mock_groq(monkeypatch, [{"offers": [
        {"id": 1, "verdict": "maybe", "reason": "Who knows."},
    ]}])

    verified, _ = verify_offers([_offer(1)], True, "key")

    assert verified[0].remote_verdict == "unconfirmed"
    assert verified[0].remote_reason == _DEGRADED_REASON


def test_extract_policy_excerpt_keeps_a_late_signal_within_budget():
    # Real shape from 2026-09-08 production data: a decisive on-site
    # statement appearing thousands of chars past where a flat prefix
    # truncation (formerly 5000 chars) would have cut the description off.
    filler = "General role description text. " * 250  # ~8250 chars
    signal = "Work Environment: this role is based onsite in our Torrance office."
    description = filler + signal + (" More filler." * 20)

    excerpt = _extract_policy_excerpt(description)

    assert "based onsite in our Torrance office" in excerpt
    assert len(excerpt) <= _MAX_DESC_CHARS


def test_extract_policy_excerpt_falls_back_to_prefix_when_no_keyword_found():
    description = "A generic role description with no stated work-location policy. " * 40
    excerpt = _extract_policy_excerpt(description)
    assert excerpt == description[:_MAX_DESC_CHARS]


def test_extract_policy_excerpt_keeps_intro_context_alongside_a_late_signal():
    intro = "We are Acme Corp, a fast-growing startup building great products."
    filler = "Team culture and mission text. " * 200
    signal = "Note: this position requires hybrid work with 3 days in the office."
    description = intro + filler + signal

    excerpt = _extract_policy_excerpt(description)

    assert "Acme Corp" in excerpt
    assert "hybrid work with 3 days in the office" in excerpt


def test_extract_policy_excerpt_merges_overlapping_keyword_windows():
    # Two nearby keyword hits ("remote" and "office") should not duplicate
    # the shared text between them.
    description = "x" * 50 + "fully remote, no office required" + "y" * 50
    excerpt = _extract_policy_excerpt(description)
    assert excerpt.count("fully remote, no office required") == 1


def test_extract_policy_excerpt_keeps_a_decisive_late_signal_among_many_hits():
    # Four early remote/office-positive keyword windows followed by the
    # sentence the verdict actually hinges on: filling the budget in document
    # order would hand the model only the positive boilerplate.
    description = (
        "Intro about us. " + "a" * 400
        + "We support remote collaboration tools." + "b" * 400
        + "Our office culture is friendly." + "c" * 400
        + "Remote-friendly benefits included." + "d" * 400
        + "Work from anywhere occasionally." + "e" * 400
        + "IMPORTANT: this role requires 4 days per week on-site in our Milan office."
    )

    excerpt = _extract_policy_excerpt(description)

    assert "4 days per week on-site" in excerpt
    assert len(excerpt) <= _MAX_DESC_CHARS


def test_extract_policy_excerpt_never_exceeds_the_budget_including_joiners():
    description = "".join(
        f"Remote work paragraph {i}. " + "z" * 500 for i in range(12)
    )

    excerpt = _extract_policy_excerpt(description)

    assert len(excerpt) <= _MAX_DESC_CHARS


def test_offers_are_verified_in_batches_of_batch_size(monkeypatch):
    monkeypatch.setattr("src.remote_verifier.time.sleep", lambda s: None)
    calls = _mock_groq(monkeypatch, [
        {"offers": [{"id": i, "verdict": "confirmed", "reason": "Remote."} for i in range(1, 21)]}
    ])

    offers = [_offer(i) for i in range(1, 21)]
    verified, _ = verify_offers(offers, True, "key")

    assert calls["count"] == math.ceil(len(offers) / BATCH_SIZE)
    assert all(o.remote_verdict == "confirmed" for o in verified)


# --- Groq daily-token-budget exhaustion (TPD) vs a transient throttle -------

def _rate_limit_error(message: str) -> groq.RateLimitError:
    return groq.RateLimitError(
        f"Error code: 429 - {{'error': {{'message': '{message}'}}}}",
        response=MagicMock(status_code=429, headers={}),
        body={"error": {"message": message}},
    )


_TPD_MESSAGE = (
    "Rate limit reached for model `openai/gpt-oss-20b` ... on tokens per day "
    "(TPD): Limit 200000, Used 199999, Requested 5000. Please try again in 4h32m."
)
_TPM_MESSAGE = (
    "Rate limit reached for model `openai/gpt-oss-20b` ... on tokens per minute "
    "(TPM): Limit 8000, Used 8000, Requested 5000. Please try again in 12s."
)


def test_is_daily_quota_exceeded_true_for_tpd_body():
    assert _is_daily_quota_exceeded(_rate_limit_error(_TPD_MESSAGE)) is True


def test_is_daily_quota_exceeded_false_for_a_per_minute_throttle():
    assert _is_daily_quota_exceeded(_rate_limit_error(_TPM_MESSAGE)) is False


def test_verify_batch_raises_immediately_on_tpd_without_burning_the_retry_ladder(monkeypatch):
    sleeps = []
    monkeypatch.setattr("src.remote_verifier.time.sleep", lambda s: sleeps.append(s))
    client = MagicMock()
    client.chat.completions.create.side_effect = _rate_limit_error(_TPD_MESSAGE)

    with pytest.raises(groq.RateLimitError):
        _verify_batch(client, [_offer(1)], True)

    # One call, no sleep: a TPD 429 resets at the next UTC day, so the 4-attempt
    # 5/10/20/40s ladder (used for a transient throttle) would only waste time.
    assert client.chat.completions.create.call_count == 1
    assert sleeps == []


def test_verify_batch_still_retries_a_transient_per_minute_throttle(monkeypatch):
    monkeypatch.setattr("src.remote_verifier.time.sleep", lambda s: None)
    client = MagicMock()
    ok_response = MagicMock()
    ok_response.choices = [MagicMock()]
    ok_response.choices[0].message.content = json.dumps(
        {"offers": [{"id": 1, "verdict": "confirmed", "reason": "Remote."}]}
    )
    ok_response.usage = MagicMock(prompt_tokens=1, completion_tokens=1, total_tokens=2)
    client.chat.completions.create.side_effect = [_rate_limit_error(_TPM_MESSAGE), ok_response]

    verdicts, _ = _verify_batch(client, [_offer(1)], True)

    assert client.chat.completions.create.call_count == 2
    assert verdicts[1].verdict == "confirmed"


# --- OpenRouter failover -----------------------------------------------------

def _mock_openrouter(monkeypatch, side_effects):
    """side_effects: list of _VerdictOutput instances or Exceptions returned by
    successive chain.invoke() calls, one per OpenRouter batch."""
    chain = MagicMock()
    chain.invoke.side_effect = side_effects
    monkeypatch.setattr("src.remote_verifier._openrouter_chain", lambda key: chain)
    return chain


def _verdict_output(items):
    return _VerdictOutput(offers=[
        {"id": i, "verdict": v, "reason": "r"} for i, v in items
    ])


def test_tpd_exhaustion_fails_over_the_rest_of_the_run_to_openrouter(monkeypatch):
    monkeypatch.setattr("src.remote_verifier.time.sleep", lambda s: None)
    # Batch 1 (offers 1-8) succeeds on Groq. Batch 2 (offers 9-16) hits the
    # daily cap immediately. Batch 3 (offers 17-20) must never call Groq at
    # all - proving the STAGE stopped, not just that one batch.
    _mock_groq(monkeypatch, [
        {"offers": [{"id": i, "verdict": "confirmed", "reason": "r"} for i in range(1, 9)]},
        _rate_limit_error(_TPD_MESSAGE),
    ])
    or_chain = _mock_openrouter(monkeypatch, [
        _verdict_output([(i, "rejected") for i in range(9, 17)]),
        _verdict_output([(i, "unconfirmed") for i in range(17, 21)]),
    ])

    offers = [_offer(i) for i in range(1, 21)]
    verified, usage = verify_offers(offers, True, "groq-key", llm_api_key="or-key")

    by_id = {o.id: o for o in verified}
    assert all(by_id[i].remote_verdict == "confirmed" for i in range(1, 9))
    assert all(by_id[i].remote_verdict == "rejected" for i in range(9, 17))
    assert all(by_id[i].remote_verdict == "unconfirmed" for i in range(17, 21))
    assert usage["provider"] == "groq+openrouter"
    assert usage["degraded"] is False
    assert or_chain.invoke.call_count == 2


def test_proactive_failover_happens_before_the_cap_is_hit(monkeypatch):
    calls = _mock_groq(monkeypatch, [{"offers": []}])
    or_chain = _mock_openrouter(monkeypatch, [_verdict_output([(1, "confirmed")])])

    groq_tokens_used_today = GROQ_DAILY_TOKEN_LIMIT - _GROQ_TPD_HEADROOM_TOKENS
    verified, usage = verify_offers(
        [_offer(1)], True, "groq-key", llm_api_key="or-key",
        groq_tokens_used_today=groq_tokens_used_today,
    )

    assert calls["count"] == 0  # Groq never called - already inside the headroom
    assert or_chain.invoke.call_count == 1
    assert verified[0].remote_verdict == "confirmed"
    assert usage["provider"] == "openrouter"
    assert usage["degraded"] is False


def test_tpd_exhaustion_without_an_openrouter_key_stops_the_stage(monkeypatch):
    """No llm_api_key configured: the pre-failover behaviour is preserved
    (mark unconfirmed, never crash), but Groq must not be called again once
    the daily cap is known to be blown."""
    monkeypatch.setattr("src.remote_verifier.time.sleep", lambda s: None)
    calls = _mock_groq(monkeypatch, [_rate_limit_error(_TPD_MESSAGE)])

    offers = [_offer(i) for i in range(1, 25)]  # 3 batches at BATCH_SIZE=8
    verified, usage = verify_offers(offers, True, "groq-key")

    # Stage-stop: only the first (failing) batch ever reaches Groq.
    assert calls["count"] == 1
    assert all(o.remote_verdict == "unconfirmed" for o in verified)
    assert usage["degraded"] is True
    assert usage["provider"] == "groq"


def test_openrouter_batch_failure_falls_back_to_unconfirmed_not_a_crash(monkeypatch):
    monkeypatch.setattr("src.remote_verifier.time.sleep", lambda s: None)
    _mock_groq(monkeypatch, [_rate_limit_error(_TPD_MESSAGE)])
    _mock_openrouter(monkeypatch, [RuntimeError("openrouter also down")])

    verified, usage = verify_offers([_offer(1)], True, "groq-key", llm_api_key="or-key")

    assert verified[0].remote_verdict == "unconfirmed"
    assert usage["degraded"] is True


def _openrouter_daily_cap_error() -> openai.RateLimitError:
    # Same shape test_scorer.py uses for a real OpenRouter daily-cap 429:
    # reset is hours away, which _is_quota_exceeded reads as "exhausted for
    # the day" rather than a per-minute throttle.
    reset_ms = int((time.time() + 3600) * 1000)
    return openai.RateLimitError(
        "rate limited",
        response=MagicMock(status_code=429, headers={"x-ratelimit-reset": str(reset_ms)}),
        body={"code": 429, "metadata": {"headers": {"X-RateLimit-Reset": str(reset_ms)}}},
    )


def test_missing_groq_key_fails_over_to_openrouter_when_configured(monkeypatch):
    """A missing Groq key is a form of "Groq unavailable", not just a
    daily-budget case - it must not skip a working OpenRouter fallback."""
    or_chain = _mock_openrouter(monkeypatch, [_verdict_output([(1, "confirmed")])])

    verified, usage = verify_offers([_offer(1)], True, "", llm_api_key="or-key")

    assert or_chain.invoke.call_count == 1
    assert verified[0].remote_verdict == "confirmed"
    assert usage["provider"] == "openrouter"
    assert usage["degraded"] is False


def test_missing_groq_key_without_openrouter_key_still_just_degrades(monkeypatch):
    # No fallback configured either: the pre-failover behaviour is preserved.
    verified, usage = verify_offers([_offer(1)], True, "")

    assert verified[0].remote_verdict == "unconfirmed"
    assert usage["degraded"] is True


def test_groq_client_build_failure_fails_over_to_openrouter_when_configured(monkeypatch):
    monkeypatch.setattr("src.remote_verifier._client",
                        lambda key: (_ for _ in ()).throw(ImportError("no groq")))
    or_chain = _mock_openrouter(monkeypatch, [_verdict_output([(1, "rejected")])])

    verified, usage = verify_offers([_offer(1)], True, "groq-key", llm_api_key="or-key")

    assert or_chain.invoke.call_count == 1
    assert verified[0].remote_verdict == "rejected"
    assert usage["degraded"] is False


def test_openrouters_own_daily_cap_stops_further_openrouter_calls(monkeypatch):
    """OpenRouter is itself a shared, budget-limited fallback (its daily
    request cap is also what scoring depends on later in the same run) - once
    its quota is known exhausted, remaining batches must not keep re-hitting
    it, the same stage-stop principle applied to Groq's TPD case."""
    monkeypatch.setattr("src.remote_verifier.time.sleep", lambda s: None)
    # Groq exhausted from the very first batch; every batch fails over.
    _mock_groq(monkeypatch, [_rate_limit_error(_TPD_MESSAGE)])
    or_chain = _mock_openrouter(monkeypatch, [_openrouter_daily_cap_error()])

    offers = [_offer(i) for i in range(1, 17)]  # 2 batches at BATCH_SIZE=8
    verified, usage = verify_offers(offers, True, "groq-key", llm_api_key="or-key")

    # Only the first batch ever reaches OpenRouter - the second batch's Groq
    # call is also skipped (groq_exhausted persists) and its OpenRouter
    # attempt is skipped too (openrouter_exhausted), not a second doomed call.
    assert or_chain.invoke.call_count == 1
    assert all(o.remote_verdict == "unconfirmed" for o in verified)
    assert usage["degraded"] is True


# --- Verification's daily share of OpenRouter requests ----------------------

def test_openrouter_failover_stops_at_its_daily_request_share(monkeypatch):
    """Scoring spends the same OpenRouter account cap later in the tier, so
    the failover stops once its share is spent, even mid-run."""
    monkeypatch.setattr("src.remote_verifier.time.sleep", lambda s: None)
    or_chain = _mock_openrouter(monkeypatch, [
        _verdict_output([(i, "confirmed") for i in range(1, 9)]),
        _verdict_output([(i, "confirmed") for i in range(9, 17)]),
    ])

    offers = [_offer(i) for i in range(1, 25)]  # 3 batches at BATCH_SIZE=8
    verified, usage = verify_offers(
        offers, True, "", llm_api_key="or-key",
        openrouter_requests_used_today=OPENROUTER_VERIFICATION_DAILY_REQUEST_CAP - 2,
    )

    assert or_chain.invoke.call_count == 2
    by_id = {o.id: o for o in verified}
    assert all(by_id[i].remote_verdict == "confirmed" for i in range(1, 17))
    assert all(by_id[i].remote_verdict == "unconfirmed" for i in range(17, 25))
    assert usage["openrouter_requests"] == 2
    assert usage["provider"] == "openrouter"
    assert usage["degraded"] is True


def test_openrouter_failover_makes_no_call_once_the_share_is_already_spent(monkeypatch):
    or_chain = _mock_openrouter(monkeypatch, [])

    verified, usage = verify_offers(
        [_offer(1)], True, "", llm_api_key="or-key",
        openrouter_requests_used_today=OPENROUTER_VERIFICATION_DAILY_REQUEST_CAP,
    )

    assert or_chain.invoke.call_count == 0
    assert verified[0].remote_verdict == "unconfirmed"
    assert usage["openrouter_requests"] == 0
    assert usage["degraded"] is True


def test_openrouter_retries_count_against_the_daily_request_share(monkeypatch):
    """A retry is a request against the account cap too: the ladder stops at
    the share instead of running past it."""
    monkeypatch.setattr("src.remote_verifier.time.sleep", lambda s: None)
    or_chain = _mock_openrouter(monkeypatch, [
        openai.APIConnectionError(request=MagicMock()),
        openai.APIConnectionError(request=MagicMock()),
        _verdict_output([(1, "confirmed")]),
    ])

    verified, usage = verify_offers(
        [_offer(1)], True, "", llm_api_key="or-key",
        openrouter_requests_used_today=OPENROUTER_VERIFICATION_DAILY_REQUEST_CAP - 2,
    )

    assert or_chain.invoke.call_count == 2
    assert verified[0].remote_verdict == "unconfirmed"
    assert usage["openrouter_requests"] == 2
    assert usage["degraded"] is True


# --- Degraded threshold: materially incomplete, not only 100% failed -------

def _mock_groq_selective(monkeypatch, should_fail):
    """should_fail(first_offer_id_in_batch) -> bool. A matching batch raises
    (retried by _verify_batch's normal ladder, still failing every attempt);
    every other batch succeeds with "confirmed" for each offer it was asked
    about. Parses ids from the prompt itself so retries within one batch
    don't drift onto a different batch's expected payload, unlike _mock_groq's
    flat sequential list."""
    def create(**kwargs):
        prompt = kwargs["messages"][0]["content"]
        ids = [int(line.split(": ")[1]) for line in prompt.split("\n") if line.startswith("ID:")]
        if should_fail(ids[0]):
            raise RuntimeError("simulated batch failure")
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = json.dumps(
            {"offers": [{"id": i, "verdict": "confirmed", "reason": "r"} for i in ids]}
        )
        response.usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        return response

    client = MagicMock()
    client.chat.completions.create = create
    monkeypatch.setattr("src.remote_verifier._client", lambda key: client)


def test_degraded_fires_well_below_total_failure(monkeypatch):
    # 20 offers = 3 batches (8+8+4). Two of three batches fail with no
    # OpenRouter fallback configured - well short of 100%, well above noise.
    monkeypatch.setattr("src.remote_verifier.time.sleep", lambda s: None)
    _mock_groq_selective(monkeypatch, should_fail=lambda first_id: first_id in (9, 17))

    verified, usage = verify_offers([_offer(i) for i in range(1, 21)], True, "key")

    assert usage["degraded"] is True


def test_a_single_stray_batch_failure_in_a_large_tier_does_not_flip_degraded(monkeypatch):
    # 160 offers = 20 batches, one fails: 5% failure, below the materiality bar.
    monkeypatch.setattr("src.remote_verifier.time.sleep", lambda s: None)
    _mock_groq_selective(monkeypatch, should_fail=lambda first_id: first_id == 1)

    verified, usage = verify_offers([_offer(i) for i in range(1, 161)], True, "key")

    assert usage["degraded"] is False


# --- No regression in the normal, budget-healthy path -----------------------

def test_healthy_run_never_touches_openrouter(monkeypatch):
    calls = _mock_groq(monkeypatch, [{"offers": [
        {"id": 1, "verdict": "confirmed", "reason": "r"},
    ]}])
    or_chain = _mock_openrouter(monkeypatch, [])

    verified, usage = verify_offers([_offer(1)], True, "groq-key", llm_api_key="or-key")

    assert calls["count"] == 1
    assert or_chain.invoke.call_count == 0
    assert usage["provider"] == "groq"
    assert usage["degraded"] is False


def test_batch_pause_is_applied_between_consecutive_groq_batches(monkeypatch):
    from src.remote_verifier import _GROQ_BATCH_PAUSE_SECONDS
    sleeps = []
    monkeypatch.setattr("src.remote_verifier.time.sleep", lambda s: sleeps.append(s))
    _mock_groq(monkeypatch, [
        {"offers": [{"id": i, "verdict": "confirmed", "reason": "r"} for i in range(1, 9)]},
    ])

    verify_offers([_offer(i) for i in range(1, 17)], True, "key")  # 2 batches

    assert sleeps.count(_GROQ_BATCH_PAUSE_SECONDS) == 1  # once, between batch 1 and 2
