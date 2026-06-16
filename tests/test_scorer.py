from unittest.mock import MagicMock
import openai
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

    result, usage = score_offers(
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
    assert "total_tokens" in usage


def test_score_offers_returns_empty_on_empty_input(monkeypatch):
    mock_chain = MagicMock()
    monkeypatch.setattr("src.scorer._build_chain", lambda _: mock_chain)

    result, usage = score_offers(
        offers=[],
        profile="p",
        priority_keywords=[],
        exclude_keywords=[],
        llm_api_key="key",
    )

    assert result == []
    assert usage["total_tokens"] == 0
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
    def invoke_side_effect(payload, **kwargs):
        batch_ids = [int(line.split(": ")[1]) for line in payload["offers"].split("\n") if line.startswith("ID:")]
        items = [s for s in scoring if s.id in batch_ids]
        call_count["n"] += 1
        return _ScoringOutput(offers=items)

    mock_chain = MagicMock()
    mock_chain.invoke.side_effect = invoke_side_effect
    monkeypatch.setattr("src.scorer._build_chain", lambda _: mock_chain)

    result, _ = score_offers(offers=offers, profile="p", priority_keywords=[], exclude_keywords=[], llm_api_key="k")

    assert mock_chain.invoke.call_count == 2  # two batches
    assert len(result) == BATCH_SIZE + 1


def test_scorer_retries_on_rate_limit(monkeypatch):
    offers = [JobOffer(id=0, title="R", company="c", link="l", description="d")]
    scoring = [_ScoringItem(id=0, score=5, comment="ok", summary="s")]

    call_count = {"n": 0}
    def invoke_side_effect(payload, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise openai.RateLimitError(
                "queue_exceeded",
                response=MagicMock(status_code=429, headers={}),
                body={},
            )
        return _ScoringOutput(offers=scoring)

    mock_chain = MagicMock()
    mock_chain.invoke.side_effect = invoke_side_effect
    monkeypatch.setattr("src.scorer._build_chain", lambda _: mock_chain)
    monkeypatch.setattr("time.sleep", lambda _: None)

    result, _ = score_offers(offers=offers, profile="p", priority_keywords=[], exclude_keywords=[], llm_api_key="k")

    assert len(result) == 1
    assert mock_chain.invoke.call_count == 3


def test_scorer_returns_partial_results_on_quota_exceeded(monkeypatch):
    from src.scorer import BATCH_SIZE
    offers = [
        JobOffer(id=i, title=f"Role {i}", company="c", link=f"l{i}", description="d")
        for i in range(BATCH_SIZE * 2)
    ]
    first_batch_scoring = [_ScoringItem(id=i, score=5, comment="ok", summary="s") for i in range(BATCH_SIZE)]

    call_count = {"n": 0}
    def invoke_side_effect(payload, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _ScoringOutput(offers=first_batch_scoring)
        raise openai.RateLimitError(
            "token_quota_exceeded",
            response=MagicMock(status_code=429, headers={}),
            body={"code": "token_quota_exceeded"},
        )

    mock_chain = MagicMock()
    mock_chain.invoke.side_effect = invoke_side_effect
    monkeypatch.setattr("src.scorer._build_chain", lambda _: mock_chain)

    result, _ = score_offers(offers=offers, profile="p", priority_keywords=[], exclude_keywords=[], llm_api_key="k")

    assert len(result) == BATCH_SIZE  # only first batch saved
    assert mock_chain.invoke.call_count == 2  # no retries on quota error


def test_scorer_truncates_long_descriptions_in_prompt(monkeypatch):
    from src.scorer import _MAX_DESC_CHARS
    long_desc = "x" * (_MAX_DESC_CHARS + 500)
    offer = JobOffer(id=0, title="Role", company="Co", link="l", description=long_desc)

    captured = {}
    def invoke_side_effect(payload, **kwargs):
        captured["offers_text"] = payload["offers"]
        return _ScoringOutput(offers=[_ScoringItem(id=0, score=5, comment="ok", summary="s")])

    mock_chain = MagicMock()
    mock_chain.invoke.side_effect = invoke_side_effect
    monkeypatch.setattr("src.scorer._build_chain", lambda _: mock_chain)

    score_offers(offers=[offer], profile="p", priority_keywords=[], exclude_keywords=[], llm_api_key="k")

    assert "x" * (_MAX_DESC_CHARS + 1) not in captured["offers_text"]
    assert offer.description == long_desc  # full description untouched on the model


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

    result, _ = score_offers(offers=offers, profile="p", priority_keywords=[], exclude_keywords=[], llm_api_key="k")

    assert [r.id for r in result] == [0, 1, 2]
