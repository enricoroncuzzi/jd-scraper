import json
from unittest.mock import MagicMock

import pytest

from src.models import JobOffer
from src.remote_verifier import verify_offers


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
