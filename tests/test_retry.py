from unittest.mock import MagicMock
import openai
import pytest
from src.retry import run_with_backoff, is_retryable, backoff_delay, DEFAULT_MAX_DELAY_SECONDS


def _quota_error():
    reset_ms = int((__import__("time").time() + 3600) * 1000)
    return openai.RateLimitError(
        "rate limited",
        response=MagicMock(status_code=429, headers={"x-ratelimit-reset": str(reset_ms)}),
        body={"code": 429, "metadata": {"headers": {"X-RateLimit-Reset": str(reset_ms)}}},
    )


def test_transient_failure_retries_with_increasing_delay():
    call_count = {"n": 0}

    def fn():
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise RuntimeError("bad LinkedIn response")

    sleeps = []
    run_with_backoff(fn, max_attempts=4, base_delay=10, sleep=sleeps.append)

    assert call_count["n"] == 3
    assert sleeps == [10, 20]  # strictly increasing, one sleep per failed attempt
    assert sleeps[0] < sleeps[1]


def test_transient_failure_retried_exhausts_attempts_and_reraises():
    def fn():
        raise ValueError("504 gateway timeout")

    give_up = MagicMock()
    with pytest.raises(ValueError):
        run_with_backoff(fn, max_attempts=3, base_delay=1, sleep=lambda _: None, on_give_up=give_up)

    exc, attempts, retryable = give_up.call_args.args
    assert isinstance(exc, ValueError)
    assert attempts == 3
    assert retryable is True


def test_quota_exhaustion_does_not_retry():
    call_count = {"n": 0}

    def fn():
        call_count["n"] += 1
        raise _quota_error()

    give_up = MagicMock()
    sleeps = []
    with pytest.raises(openai.RateLimitError):
        run_with_backoff(fn, max_attempts=5, base_delay=1, sleep=sleeps.append, on_give_up=give_up)

    assert call_count["n"] == 1  # no retry loop at all
    assert sleeps == []
    exc, attempts, retryable = give_up.call_args.args
    assert attempts == 1
    assert retryable is False


def test_success_after_transient_failure_does_not_give_up():
    call_count = {"n": 0}

    def fn():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("transient")

    give_up = MagicMock()
    run_with_backoff(fn, max_attempts=3, base_delay=1, sleep=lambda _: None, on_give_up=give_up)

    assert call_count["n"] == 2
    give_up.assert_not_called()


def test_is_retryable_false_for_quota_error():
    assert is_retryable(_quota_error()) is False


def test_is_retryable_true_for_non_quota_rate_limit_error():
    e = openai.RateLimitError(
        "rate limited",
        response=MagicMock(status_code=429, headers={}),
        body={"code": 429},
    )
    assert is_retryable(e) is True


def test_is_retryable_true_for_generic_exceptions():
    assert is_retryable(RuntimeError("boom")) is True
    assert is_retryable(ValueError("boom")) is True


def test_backoff_delay_is_capped():
    assert backoff_delay(20, base=60, cap=DEFAULT_MAX_DELAY_SECONDS) == DEFAULT_MAX_DELAY_SECONDS
