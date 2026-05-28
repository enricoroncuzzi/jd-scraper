from unittest.mock import MagicMock
from src.models import JobOffer, ScoredOffer, RankedOffers
from src.scorer import score_offers


def _make_offers():
    return [
        JobOffer(id=0, title="AI Engineer", company="Acme",
                 location="Milan", link="https://li.com/1",
                 description="LLM pipeline, RAG, FastAPI"),
        JobOffer(id=1, title="Java Developer", company="Corp",
                 location="London", link="https://li.com/2",
                 description=""),
    ]


def _make_mock_chain(scored_offers):
    chain = MagicMock()
    chain.invoke.return_value = RankedOffers(offers=scored_offers)
    return chain


def test_score_offers_returns_scored_offers(monkeypatch):
    expected = [
        ScoredOffer(id=0, title="AI Engineer", company="Acme",
                    location="Milan", link="https://li.com/1",
                    description="LLM pipeline, RAG, FastAPI",
                    score=9, comment="Strong LLM/RAG fit", summary="LLM role"),
        ScoredOffer(id=1, title="Java Developer", company="Corp",
                    location="London", link="https://li.com/2",
                    description="",
                    score=1, comment="Description unavailable — could not evaluate.",
                    summary=""),
    ]
    monkeypatch.setattr("src.scorer._build_chain", lambda _: _make_mock_chain(expected))

    result = score_offers(
        offers=_make_offers(),
        profile="test profile",
        priority_keywords=["LLM"],
        exclude_keywords=["Java"],
        groq_api_key="test-key",
    )

    assert len(result) == 2
    assert result[0].score == 9
    assert result[1].score == 1
    assert result[1].comment == "Description unavailable — could not evaluate."


def test_score_offers_returns_empty_on_empty_input(monkeypatch):
    mock_chain = MagicMock()
    monkeypatch.setattr("src.scorer._build_chain", lambda _: mock_chain)

    result = score_offers(
        offers=[],
        profile="p",
        priority_keywords=[],
        exclude_keywords=[],
        groq_api_key="key",
    )

    assert result == []
    mock_chain.invoke.assert_not_called()


def test_score_offers_preserves_offer_order(monkeypatch):
    offers = [
        JobOffer(id=0, title="A", company="c", link="l0", description="d"),
        JobOffer(id=1, title="B", company="c", link="l1", description="d"),
        JobOffer(id=2, title="C", company="c", link="l2", description="d"),
    ]
    scored = [
        ScoredOffer(id=2, title="C", company="c", link="l2", score=7, comment="ok", summary="s"),
        ScoredOffer(id=0, title="A", company="c", link="l0", score=9, comment="ok", summary="s"),
        ScoredOffer(id=1, title="B", company="c", link="l1", score=5, comment="ok", summary="s"),
    ]
    monkeypatch.setattr("src.scorer._build_chain", lambda _: _make_mock_chain(scored))

    result = score_offers(offers=offers, profile="p", priority_keywords=[], exclude_keywords=[], groq_api_key="k")

    assert [r.id for r in result] == [0, 1, 2]
