import time
from unittest.mock import MagicMock
import openai
from src.models import JobOffer, ScoredOffer
from src.scorer import (
    score_offers,
    _ScoringItem,
    _ScoringOutput,
    _is_quota_exceeded,
    _is_retryable_upstream_value_error,
    _OPENROUTER_FALLBACK_MODELS,
)


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


def test_scorer_retries_on_internal_server_error(monkeypatch):
    # A real HTTP 5xx from OpenRouter surfaces via the openai SDK as
    # openai.InternalServerError - confirm the batch retries instead of
    # propagating (which would trigger a full tier restart upstream).
    offers = [JobOffer(id=0, title="R", company="c", link="l", description="d")]
    scoring = [_ScoringItem(id=0, score=5, comment="ok", summary="s")]

    call_count = {"n": 0}
    def invoke_side_effect(payload, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise openai.InternalServerError(
                "Gateway Timeout",
                response=MagicMock(status_code=504, headers={}),
                body={"code": 504, "message": "Gateway Timeout"},
            )
        return _ScoringOutput(offers=scoring)

    mock_chain = MagicMock()
    mock_chain.invoke.side_effect = invoke_side_effect
    monkeypatch.setattr("src.scorer._build_chain", lambda _: mock_chain)
    monkeypatch.setattr("time.sleep", lambda _: None)

    result, _ = score_offers(offers=offers, profile="p", priority_keywords=[], exclude_keywords=[], llm_api_key="k")

    assert len(result) == 1
    assert mock_chain.invoke.call_count == 3


def test_scorer_retries_on_api_connection_error(monkeypatch):
    offers = [JobOffer(id=0, title="R", company="c", link="l", description="d")]
    scoring = [_ScoringItem(id=0, score=5, comment="ok", summary="s")]

    call_count = {"n": 0}
    def invoke_side_effect(payload, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise openai.APIConnectionError(message="Connection timed out", request=MagicMock())
        return _ScoringOutput(offers=scoring)

    mock_chain = MagicMock()
    mock_chain.invoke.side_effect = invoke_side_effect
    monkeypatch.setattr("src.scorer._build_chain", lambda _: mock_chain)
    monkeypatch.setattr("time.sleep", lambda _: None)

    result, _ = score_offers(offers=offers, profile="p", priority_keywords=[], exclude_keywords=[], llm_api_key="k")

    assert len(result) == 1
    assert mock_chain.invoke.call_count == 3


def test_scorer_retries_on_openrouter_200_error_body_504(monkeypatch):
    # OpenRouter's actual observed shape for an upstream 504: HTTP 200 with a
    # JSON error body, which langchain_openai surfaces as a plain ValueError
    # (openai.InternalServerError never fires since the status is 200).
    offers = [JobOffer(id=0, title="R", company="c", link="l", description="d")]
    scoring = [_ScoringItem(id=0, score=5, comment="ok", summary="s")]

    call_count = {"n": 0}
    def invoke_side_effect(payload, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise ValueError({"code": 504, "message": "Gateway Timeout: upstream took too long"})
        return _ScoringOutput(offers=scoring)

    mock_chain = MagicMock()
    mock_chain.invoke.side_effect = invoke_side_effect
    monkeypatch.setattr("src.scorer._build_chain", lambda _: mock_chain)
    monkeypatch.setattr("time.sleep", lambda _: None)

    result, _ = score_offers(offers=offers, profile="p", priority_keywords=[], exclude_keywords=[], llm_api_key="k")

    assert len(result) == 1
    assert mock_chain.invoke.call_count == 3


def test_scorer_propagates_non_retryable_value_error(monkeypatch):
    # A ValueError unrelated to an upstream 5xx (e.g. a genuine bug/malformed
    # response) must NOT be swallowed as a retryable upstream error.
    offers = [JobOffer(id=0, title="R", company="c", link="l", description="d")]

    mock_chain = MagicMock()
    mock_chain.invoke.side_effect = ValueError({"code": "bad_request", "message": "Invalid schema"})
    monkeypatch.setattr("src.scorer._build_chain", lambda _: mock_chain)
    monkeypatch.setattr("time.sleep", lambda _: None)

    try:
        score_offers(offers=offers, profile="p", priority_keywords=[], exclude_keywords=[], llm_api_key="k")
        assert False, "expected ValueError to propagate"
    except ValueError:
        pass

    assert mock_chain.invoke.call_count == 1  # not retried


def test_scorer_returns_partial_results_on_upstream_error_retries_exhausted(monkeypatch):
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
        raise openai.InternalServerError(
            "Service Unavailable",
            response=MagicMock(status_code=503, headers={}),
            body={"code": 503},
        )

    mock_chain = MagicMock()
    mock_chain.invoke.side_effect = invoke_side_effect
    monkeypatch.setattr("src.scorer._build_chain", lambda _: mock_chain)
    monkeypatch.setattr("time.sleep", lambda _: None)

    result, _ = score_offers(offers=offers, profile="p", priority_keywords=[], exclude_keywords=[], llm_api_key="k")

    assert len(result) == BATCH_SIZE  # only first batch saved, no tier-level propagation


def test_is_retryable_upstream_value_error_true_for_5xx_code():
    e = ValueError({"code": 504, "message": "Gateway Timeout"})
    assert _is_retryable_upstream_value_error(e) is True


def test_is_retryable_upstream_value_error_true_for_timeout_keyword():
    e = ValueError({"code": "unknown", "message": "upstream request timed out"})
    assert _is_retryable_upstream_value_error(e) is True


def test_is_retryable_upstream_value_error_false_for_unrelated_error():
    e = ValueError({"code": "bad_request", "message": "Invalid schema for tool call"})
    assert _is_retryable_upstream_value_error(e) is False


def test_is_retryable_upstream_value_error_false_for_empty_args():
    e = ValueError()
    assert _is_retryable_upstream_value_error(e) is False


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


def test_is_quota_exceeded_true_for_legacy_cerebras_shape():
    e = openai.RateLimitError(
        "token_quota_exceeded",
        response=MagicMock(status_code=429, headers={}),
        body={"code": "token_quota_exceeded"},
    )
    assert _is_quota_exceeded(e) is True


def test_is_quota_exceeded_false_for_openrouter_per_minute_throttle():
    # Real shape observed from OpenRouter: reset is seconds away (per-minute window).
    reset_ms = int((time.time() + 30) * 1000)
    e = openai.RateLimitError(
        "rate limited",
        response=MagicMock(status_code=429, headers={"x-ratelimit-reset": str(reset_ms)}),
        body={"code": 429, "metadata": {"headers": {"X-RateLimit-Reset": str(reset_ms)}}},
    )
    assert _is_quota_exceeded(e) is False


def test_is_quota_exceeded_true_for_openrouter_daily_cap():
    # Real shape observed from OpenRouter: reset is hours away (daily free-tier cap).
    reset_ms = int((time.time() + 3600) * 1000)
    e = openai.RateLimitError(
        "rate limited",
        response=MagicMock(status_code=429, headers={"x-ratelimit-reset": str(reset_ms)}),
        body={"code": 429, "metadata": {"headers": {"X-RateLimit-Reset": str(reset_ms)}}},
    )
    assert _is_quota_exceeded(e) is True


def test_is_quota_exceeded_false_for_upstream_provider_busy():
    # Real shape observed from OpenRouter when an underlying free model is
    # temporarily overloaded (no X-RateLimit-Reset header at all).
    e = openai.RateLimitError(
        "rate limited",
        response=MagicMock(status_code=429, headers={}),
        body={"code": 429, "metadata": {"provider_error_code": "upstream_429", "retry_after_seconds": 5}},
    )
    assert _is_quota_exceeded(e) is False


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


def test_openrouter_fallback_models_within_array_size_cap():
    # OpenRouter rejects extra_body["models"] with a 400 ("'models' array must
    # have 3 items or fewer") above this size. A 6-entry list landed in
    # 2026-09-01's diversification and broke every scoring request for 2 days
    # before being caught - this guards against that regression recurring.
    assert len(_OPENROUTER_FALLBACK_MODELS) <= 3
