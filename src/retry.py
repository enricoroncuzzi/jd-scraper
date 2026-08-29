import time
from typing import Callable
import openai
from src.scorer import _is_quota_exceeded

DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BASE_DELAY_SECONDS = 60
DEFAULT_MAX_DELAY_SECONDS = 900  # 15 min cap


def is_retryable(exc: Exception) -> bool:
    """Quota exhaustion is never worth retrying: it just burns more of an
    already-exhausted daily budget. Everything else (the scraper RuntimeError,
    a non-quota OpenRouter error, etc.) is a transient failure worth retrying.
    Reuses src.scorer._is_quota_exceeded so retry policy and quota detection
    never diverge into two separate opinions about what a quota failure looks like."""
    return not (isinstance(exc, openai.RateLimitError) and _is_quota_exceeded(exc))


def backoff_delay(attempt: int, base: float = DEFAULT_BASE_DELAY_SECONDS, cap: float = DEFAULT_MAX_DELAY_SECONDS) -> float:
    return min(base * (2 ** attempt), cap)


def run_with_backoff(
    fn: Callable[[], None],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY_SECONDS,
    max_delay: float = DEFAULT_MAX_DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[Exception, int, float], None] | None = None,
    on_give_up: Callable[[Exception, int, bool], None] | None = None,
) -> None:
    """Run fn(), retrying with exponential backoff on transient failures.

    on_retry(exc, attempt_number, delay_seconds) fires before each retry sleep.
    on_give_up(exc, attempts_made, retryable) fires once, right before the
    final exception is re-raised: either because the failure was non-retryable
    (quota exhaustion) or because max_attempts was reached.
    """
    for attempt in range(max_attempts):
        try:
            fn()
            return
        except Exception as exc:
            retryable = is_retryable(exc)
            out_of_attempts = attempt == max_attempts - 1
            if not retryable or out_of_attempts:
                if on_give_up:
                    on_give_up(exc, attempt + 1, retryable)
                raise
            delay = backoff_delay(attempt, base_delay, max_delay)
            if on_retry:
                on_retry(exc, attempt + 1, delay)
            sleep(delay)
