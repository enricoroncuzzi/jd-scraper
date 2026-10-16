"""Time windows and walk cadence for LinkedIn slices (spec section 4.4).

LinkedIn's guest search only has a lower time bound (f_TPR=r<seconds>), so a
window always reaches back from now. Walking a slice again sooner is the only
way to look at a smaller span, which is what halving does.

The adaptive cadence is bounded: no search area may go longer than 48 h between
walks, and no interval is ever shortened below 12 h. A walk stopped by its
area's page cap keeps the base cadence (it doubles back toward the base, never
halves), so the ten capped countries are not walked four times more often.
"""
from datetime import datetime, timedelta

MARGIN_SECONDS = 2 * 3600
FIRST_WALK_WINDOW_SECONDS = 24 * 3600
MAX_WINDOW_SECONDS = 7 * 24 * 3600
DEFAULT_MIN_INTERVAL_SECONDS = 12 * 3600
DEFAULT_MAX_INTERVAL_SECONDS = 48 * 3600
EMPTY_SLICE_INTERVAL_SECONDS = 24 * 3600
EMPTY_STREAK_FOR_DAILY = 3
RETRY_SOON_SECONDS = 30 * 60

# Walks that did not finish the slice come back soon without touching the cadence.
_RETRY_SOON_REASONS = frozenset({"throttled_empty", "rate_limited", "error"})


def window_seconds(now: datetime, last_walked_at: datetime | None, *,
                   margin: int = MARGIN_SECONDS, first: int = FIRST_WALK_WINDOW_SECONDS,
                   cap: int = MAX_WINDOW_SECONDS) -> int:
    """Seconds since the last walk started plus a safety margin, so a restart or a
    pause leaves no gap. A slice never walked looks back `first` seconds."""
    if last_walked_at is None:
        return first
    elapsed = max(0, int((now - last_walked_at).total_seconds()))
    return min(elapsed + margin, cap)


def f_tpr(seconds: int) -> str:
    return f"r{seconds}"


def next_interval(*, base: int, current: int, stop_reason: str, consecutive_empty: int,
                  min_interval: int = DEFAULT_MIN_INTERVAL_SECONDS,
                  max_interval: int = DEFAULT_MAX_INTERVAL_SECONDS) -> int:
    """How long to wait before the next walk.

    A walk that hit the full page cap halves the interval so the next window is
    smaller; an area cap doubles it back toward the base like any finished walk.
    A slice that keeps coming back empty is walked at most daily. The result is
    always inside [min_interval, max_interval].
    """
    if stop_reason == "cap_hit":
        interval = max(current // 2, min_interval)
    elif stop_reason in _RETRY_SOON_REASONS:
        interval = current
    else:
        interval = min(current * 2, base)
    if consecutive_empty >= EMPTY_STREAK_FOR_DAILY:
        interval = max(interval, EMPTY_SLICE_INTERVAL_SECONDS)
    return max(min_interval, min(interval, max_interval))


def next_due(now: datetime, interval: int, stop_reason: str) -> datetime:
    if stop_reason in _RETRY_SOON_REASONS:
        return now + timedelta(seconds=RETRY_SOON_SECONDS)
    return now + timedelta(seconds=interval)
