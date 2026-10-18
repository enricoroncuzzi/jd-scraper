"""Every verdict path of the two-field remote answer, without any live model.

The prompt asks for work_mode + eligibility + a quote from the description; the
code maps that to the verdict vocabulary. These tests fix that mapping, including
the D3 rule (eligibility limits never reject) and the quote check (an unsupported
quote is not a verdict).
"""
import json
from unittest.mock import MagicMock

import pytest

from src.models import JobOffer
from src.remote_rules import classify
from src.remote_verifier import (
    _PROMPT_DESCRIPTION_BUDGET_CHARS,
    _description_for_prompt,
    _per_offer_budget,
    _quote_found,
    verify_offers,
)

DESCRIPTION = ("Acme builds developer tools. This role is fully remote across Europe. "
               "You will work with Python and PyTorch on retrieval pipelines every day.")


def _offer(offer_id=1, description=DESCRIPTION):
    return JobOffer(id=offer_id, title="AI Engineer", company="Acme", location="Berlin, Germany",
                    link=f"https://x/{offer_id}", description=description,
                    description_status="ok")


def _mock(monkeypatch, items):
    seen = {"prompts": []}

    def create(**kwargs):
        seen["prompts"].append(kwargs["messages"][0]["content"])
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = json.dumps({"offers": items})
        response.usage = MagicMock(prompt_tokens=100, completion_tokens=20, total_tokens=120)
        return response

    client = MagicMock()
    client.chat.completions.create = create
    monkeypatch.setattr("src.remote_verifier._client", lambda key: client)
    return seen


def _run(monkeypatch, item, *, require_italy, description=DESCRIPTION, offer=None):
    items = [{"id": 1, **item}]
    _mock(monkeypatch, items)
    verified, _usage = verify_offers([offer or _offer(description=description)],
                                     require_italy, "key")
    return verified[0]


def test_remote_open_is_confirmed(monkeypatch):
    job = _run(monkeypatch, {"work_mode": "remote", "eligibility": "open",
                             "quote": "fully remote across Europe"},
               require_italy=True)
    assert (job.remote_verdict, job.remote_advice) == ("confirmed", "")


def test_remote_first_counts_as_remote(monkeypatch):
    description = "We are remote-first, with an office in Berlin if you want it."
    job = _run(monkeypatch, {"work_mode": "remote", "eligibility": "open",
                             "quote": "remote-first"},
               require_italy=True, description=description)
    assert job.remote_verdict == "confirmed"


def test_remote_limited_to_another_country_is_advised_never_rejected(monkeypatch):
    description = "We are fully remote. Remote (UK) only."
    job = _run(monkeypatch, {"work_mode": "remote", "eligibility": "restricted",
                             "quote": "Remote (UK) only"},
               require_italy=True, description=description)
    assert job.remote_verdict == "advised"
    assert job.remote_advice == "remote, limited to the UK - check eligibility"


def test_remote_without_a_stated_restriction_is_advised_in_tier_three(monkeypatch):
    offer = _offer()
    offer.location = "Remote"
    job = _run(monkeypatch, {"work_mode": "remote", "eligibility": "not_stated",
                             "quote": "fully remote across Europe"},
               require_italy=True, offer=offer)
    assert job.remote_verdict == "advised"
    assert job.remote_advice == "remote, no stated country restriction - check eligibility"


@pytest.mark.parametrize("require_italy", [True, False])
def test_plain_remote_gets_the_same_verdict_from_the_rules_and_the_model(monkeypatch, require_italy):
    """D3: plain 'Remote' with no country named is advised in tiers 3/4 and
    confirmed in tier 1, whichever layer reaches it first."""
    description = ("This is a fully remote role. You will build retrieval pipelines with "
                   "Python and PyTorch and ship evaluation tooling for our product teams.")
    offer = _offer(description=description)
    offer.location = "London, United Kingdom"
    ruled = classify(description, title=offer.title, location=offer.location,
                     require_italy_eligibility=require_italy)
    # The answer the prompt asks for on this posting: remote, no country stated.
    job = _run(monkeypatch, {"work_mode": "remote", "eligibility": "not_stated",
                             "quote": "fully remote role"},
               require_italy=require_italy, offer=offer)
    assert (job.remote_verdict, job.remote_advice) == (ruled.verdict, ruled.advice)
    assert job.remote_verdict == ("advised" if require_italy else "confirmed")


def test_a_remote_role_restricted_to_italy_is_confirmed(monkeypatch):
    description = "Fully remote, open to residents of Italy only. Our HQ is in London, UK."
    job = _run(monkeypatch, {"work_mode": "remote", "eligibility": "restricted",
                             "quote": "open to residents of Italy only"},
               require_italy=True, description=description)
    assert (job.remote_verdict, job.remote_advice) == ("confirmed", "")


@pytest.mark.parametrize("description,advice", [
    ("ABOUT US: we build developer tools. Fully remote. Candidates must live in Spain.",
     "remote, limited to Spain - check eligibility"),
    ("Many of us work in Poland. Fully remote. Offices in Italy. You must reside in Poland.",
     "remote, limited to Poland - check eligibility"),
    ("Fully remote. We serve clients in Germany and France. Eligibility is limited.",
     "remote, based in Germany - check eligibility"),
    ("Fully remote. Our HQ is in Amsterdam, Netherlands. You must be eligible to work in "
     "the EU.", "remote, based in Germany - check eligibility"),
])
def test_a_restricted_answer_reads_the_country_from_the_restriction_sentence(
        monkeypatch, description, advice):
    job = _run(monkeypatch, {"work_mode": "remote", "eligibility": "restricted",
                             "quote": "Fully remote"},
               require_italy=True, description=description)
    assert (job.remote_verdict, job.remote_advice) == ("advised", advice)


@pytest.mark.parametrize("description,verdict,advice", [
    ("Remote. Open to candidates living in Italy only. We serve clients across Europe.",
     "confirmed", ""),
    ("Remote. Open to candidates living in Germany only. We serve clients across Europe.",
     "advised", "remote, limited to Germany - check eligibility"),
])
def test_a_restricted_answer_confirms_italy_and_advises_another_country(
        monkeypatch, description, verdict, advice):
    job = _run(monkeypatch, {"work_mode": "remote", "eligibility": "restricted",
                             "quote": "Open to candidates living in"},
               require_italy=True, description=description)
    assert (job.remote_verdict, job.remote_advice) == (verdict, advice)


def test_a_quote_taken_from_the_title_or_location_is_found(monkeypatch):
    offer = JobOffer(id=1, title="Backend Engineer - REMOTE", company="Acme",
                     location="Italy (Remote)", link="https://x/1",
                     description="Great team, smart working and flexible hours. " * 5,
                     description_status="ok")
    job = _run(monkeypatch, {"work_mode": "remote", "eligibility": "open", "quote": "REMOTE"},
               require_italy=False, offer=offer)
    assert job.remote_verdict == "confirmed"
    job = _run(monkeypatch, {"work_mode": "remote", "eligibility": "open",
                             "quote": "Italy (Remote)"},
               require_italy=True, offer=offer.model_copy(update={"remote_verdict": None}))
    assert job.remote_verdict == "confirmed"


def test_tier_one_does_not_need_the_eligibility_answer(monkeypatch):
    job = _run(monkeypatch, {"work_mode": "remote", "eligibility": "restricted",
                             "quote": "fully remote across Europe"},
               require_italy=False)
    assert (job.remote_verdict, job.remote_advice) == ("confirmed", "")


@pytest.mark.parametrize("work_mode", ["hybrid", "onsite", "flexible"])
def test_hybrid_on_site_and_perk_remote_are_rejected_with_a_found_quote(monkeypatch, work_mode):
    job = _run(monkeypatch, {"work_mode": work_mode, "eligibility": "not_stated",
                             "quote": "fully remote across Europe"},
               require_italy=False)
    assert job.remote_verdict == "rejected"


def test_a_rejection_whose_quote_is_not_in_the_text_is_not_rejected(monkeypatch):
    job = _run(monkeypatch, {"work_mode": "hybrid", "eligibility": "not_stated",
                             "quote": "three days a week in the London office"},
               require_italy=False)
    assert job.remote_verdict == "not_stated"


def test_a_confirmation_whose_quote_is_not_in_the_text_is_not_stated(monkeypatch):
    job = _run(monkeypatch, {"work_mode": "remote", "eligibility": "open",
                             "quote": "work from anywhere on the planet"},
               require_italy=False)
    assert job.remote_verdict == "not_stated"


def test_an_empty_quote_is_not_stated(monkeypatch):
    job = _run(monkeypatch, {"work_mode": "remote", "eligibility": "open", "quote": ""},
               require_italy=False)
    assert job.remote_verdict == "not_stated"


def test_not_stated_is_stored_as_its_own_verdict(monkeypatch):
    job = _run(monkeypatch, {"work_mode": "not_stated", "eligibility": "not_stated",
                             "quote": "", "reason": "The posting never says."},
               require_italy=False)
    assert job.remote_verdict == "not_stated"
    assert job.remote_reason == "The posting never says."


def test_quote_matching_survives_glued_words_and_punctuation():
    # The stored text loses spaces ("LocationRemote, Europe"); the model quotes
    # the readable form. Both normalize to the same key.
    assert _quote_found("Location: Remote, Europe", "LocationRemote, Europe - fully remote")
    assert _quote_found("FULLY REMOTE", "This is a Fully-Remote role.")
    assert not _quote_found("three days in the office", DESCRIPTION)


def test_the_whole_description_reaches_the_model_not_a_1000_char_prefix(monkeypatch):
    description = ("Acme builds developer tools. " * 60) + " This role is fully remote."
    assert len(description) > 1500
    seen = _mock(monkeypatch, [{"id": 1, "work_mode": "remote", "eligibility": "open",
                                "quote": "fully remote"}])
    verify_offers([_offer(description=description)], True, "key")
    assert "This role is fully remote." in seen["prompts"][0]
    assert "fully remote" in seen["prompts"][0]


def test_the_prompt_is_bounded_for_an_absurd_description():
    long = ("remote " * 5000) + "fully remote"
    assert len(_description_for_prompt(long)) <= 6000


def test_a_full_batch_of_long_descriptions_stays_inside_the_shared_budget():
    intro = "Acme builds developer tools for retrieval and evaluation. " * 60
    late = " Work mode: this is a hybrid role with two days in the Milan office." * 3
    budget = _per_offer_budget(8)
    shown = [_description_for_prompt(intro + late + f" offer {n}", budget) for n in range(8)]
    assert all("hybrid role" in text for text in shown)
    assert sum(len(text) for text in shown) <= _PROMPT_DESCRIPTION_BUDGET_CHARS


def test_a_full_batch_shares_one_description_budget_for_groq():
    """An 8-offer batch must stay inside Groq's 8,000-token/minute free plan."""
    assert _per_offer_budget(8) == 3000
    assert _per_offer_budget(1) == 6000
    description = ("word " * 600) + "SENTINEL-LATE " + ("word " * 400)
    assert len(description) > 5000
    assert "SENTINEL-LATE" not in _description_for_prompt(description, _per_offer_budget(8))
    assert "SENTINEL-LATE" in _description_for_prompt(description, _per_offer_budget(1))
