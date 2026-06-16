from unittest.mock import MagicMock
from src.models import JobOffer, ScoredOffer
from src.scorer import score_offers, _ScoringItem, _ScoringOutput


def _make_offers():
    return [
        JobOffer(id=0, title="AI Engineer", company="Acme",
                 location="Milan", link="https://li.com/1",
                 description="LLM pipeline, RAG, FastAPI"),
        JobOffer(id=1, title="Java Developer", company="Corp",
                 location="London", link="https://li.com/2",
                 description=""),
    ]


def _make_mock_chain(scoring_items):
    chain = MagicMock()
    chain.invoke.return_value = _ScoringOutput(offers=scoring_items)
    return chain


def test_score_offers_returns_scored_offers(monkeypatch):
    scoring = [
        _ScoringItem(id=0, score=9, comment="Strong LLM/RAG fit", summary="LLM role"),
        _ScoringItem(id=1, score=1, comment="Description unavailable — could not evaluate.", summary=""),
    ]
    monkeypatch.setattr("src.scorer._build_chain", lambda _: _make_mock_chain(scoring))

    result = score_offers(
        offers=_make_offers(),
        profile="test profile",
        priority_keywords=["LLM"],
        exclude_keywords=["Java"],
        llm_api_key="test-key",
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
        llm_api_key="key",
    )

    assert result == []
    mock_chain.invoke.assert_not_called()


def test_score_offers_batches_large_input(monkeypatch):
    from src.scorer import BATCH_SIZE
    # Create BATCH_SIZE + 1 offers to force two batches
    offers = [
        JobOffer(id=i, title=f"Role {i}", company="c", link=f"l{i}", description="d")
        for i in range(BATCH_SIZE + 1)
    ]
    scoring = [_ScoringItem(id=i, score=5, comment="ok", summary="s") for i in range(BATCH_SIZE + 1)]

    # Side-effect: return first BATCH_SIZE items on first call, remaining on second
    call_count = {"n": 0}
    def invoke_side_effect(payload):
        batch_ids = [int(line.split(": ")[1]) for line in payload["offers"].split("\n") if line.startswith("ID:")]
        items = [s for s in scoring if s.id in batch_ids]
        call_count["n"] += 1
        return _ScoringOutput(offers=items)

    mock_chain = MagicMock()
    mock_chain.invoke.side_effect = invoke_side_effect
    monkeypatch.setattr("src.scorer._build_chain", lambda _: mock_chain)

    result = score_offers(offers=offers, profile="p", priority_keywords=[], exclude_keywords=[], llm_api_key="k")

    assert mock_chain.invoke.call_count == 2  # two batches
    assert len(result) == BATCH_SIZE + 1



def test_score_offers_preserves_offer_order(monkeypatch):
    offers = [
        JobOffer(id=0, title="A", company="c", link="l0", description="d"),
        JobOffer(id=1, title="B", company="c", link="l1", description="d"),
        JobOffer(id=2, title="C", company="c", link="l2", description="d"),
    ]
    scoring = [
        _ScoringItem(id=2, score=7, comment="ok", summary="s"),
        _ScoringItem(id=0, score=9, comment="ok", summary="s"),
        _ScoringItem(id=1, score=5, comment="ok", summary="s"),
    ]
    monkeypatch.setattr("src.scorer._build_chain", lambda _: _make_mock_chain(scoring))

    result = score_offers(offers=offers, profile="p", priority_keywords=[], exclude_keywords=[], llm_api_key="k")

    assert [r.id for r in result] == [0, 1, 2]
