import pytest
from typing import Literal
from pydantic import ValidationError
from src.models import JobOffer, ScoredOffer, RankedOffers


def test_job_offer_optional_fields_have_defaults():
    offer = JobOffer(id=1, title="AI Engineer", company="Acme", link="https://li.com/1")
    assert offer.location == "N/A"
    assert offer.description == ""
    assert offer.work_mode == ""


def test_job_offer_accepts_all_fields():
    offer = JobOffer(id=1, title="AI Engineer", company="Acme",
                     location="Milan", link="https://li.com/1",
                     description="some text")
    assert offer.description == "some text"
    assert offer.location == "Milan"


def test_scored_offer_inherits_job_offer_fields():
    offer = ScoredOffer(id=1, title="AI Engineer", company="Acme",
                        link="https://li.com/1", score=8)
    assert offer.location == "N/A"
    assert offer.description == ""
    assert offer.comment == ""
    assert offer.summary == ""
    assert offer.score == 8


def test_scored_offer_score_above_10_is_invalid():
    with pytest.raises(ValidationError):
        ScoredOffer(id=1, title="t", company="c", link="l", score=11)


def test_scored_offer_score_below_1_is_invalid():
    with pytest.raises(ValidationError):
        ScoredOffer(id=1, title="t", company="c", link="l", score=0)


def test_ranked_offers_wraps_list():
    offers = [ScoredOffer(id=1, title="t", company="c", link="l", score=5)]
    ranked = RankedOffers(offers=offers)
    assert len(ranked.offers) == 1
    assert ranked.offers[0].score == 5


def test_job_offer_description_status_defaults_to_ok():
    offer = JobOffer(id=1, title="AI Engineer", company="Acme", link="https://li.com/1")
    assert offer.description_status == "ok"


def test_job_offer_description_status_accepts_valid_values():
    for status in ("ok", "partial", "failed"):
        offer = JobOffer(id=1, title="t", company="c", link="l", description_status=status)
        assert offer.description_status == status


def test_scored_offer_inherits_description_status():
    offer = ScoredOffer(id=1, title="t", company="c", link="l", score=7, description_status="partial")
    assert offer.description_status == "partial"


def test_job_offer_defaults_to_not_checked():
    offer = JobOffer(id=1, title="AI Engineer", company="Acme", link="https://x/1")
    assert offer.remote_verdict == "not_checked"
    assert offer.remote_reason == ""


def test_scored_offer_carries_verdict():
    offer = ScoredOffer(
        id=1, title="AI Engineer", company="Acme", link="https://x/1", score=9,
        remote_verdict="confirmed", remote_reason="States remote anywhere in the EU.",
    )
    assert offer.remote_verdict == "confirmed"
    assert offer.remote_reason == "States remote anywhere in the EU."
