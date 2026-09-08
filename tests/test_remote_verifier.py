import json
from unittest.mock import MagicMock

import pytest

from src.models import JobOffer
from src.remote_verifier import _DEGRADED_REASON, _MAX_DESC_CHARS, _extract_policy_excerpt, verify_offers


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


def test_batch_size_is_larger_than_the_scorers_to_amortize_prompt_overhead():
    from src.remote_verifier import BATCH_SIZE
    from src.scorer import BATCH_SIZE as SCORER_BATCH_SIZE
    assert BATCH_SIZE > SCORER_BATCH_SIZE
