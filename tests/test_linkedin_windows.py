from datetime import datetime, timedelta, timezone

import pytest

from src.linkedin.windows import (
    EMPTY_SLICE_INTERVAL_SECONDS, FIRST_WALK_WINDOW_SECONDS, MARGIN_SECONDS, MAX_WINDOW_SECONDS,
    MIN_INTERVAL_SECONDS, RETRY_SOON_SECONDS, f_tpr, next_due, next_interval, window_seconds,
)

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
H = 3600


def test_a_slice_never_walked_looks_back_24_hours():
    assert window_seconds(NOW, None) == FIRST_WALK_WINDOW_SECONDS == 86400


def test_the_window_is_the_time_since_the_last_walk_plus_two_hours():
    assert window_seconds(NOW, NOW - timedelta(hours=4)) == 4 * H + MARGIN_SECONDS


def test_a_long_pause_is_clamped_to_seven_days():
    assert window_seconds(NOW, NOW - timedelta(days=30)) == MAX_WINDOW_SECONDS


def test_a_clock_that_ran_backwards_still_gives_the_margin():
    assert window_seconds(NOW, NOW + timedelta(minutes=5)) == MARGIN_SECONDS


def test_f_tpr_format():
    assert f_tpr(21600) == "r21600"


def test_cap_hit_halves_the_interval_down_to_an_hour():
    assert next_interval(base=12 * H, current=12 * H, stop_reason="cap_hit", consecutive_empty=0) == 6 * H
    assert next_interval(base=12 * H, current=2 * H, stop_reason="cap_hit", consecutive_empty=0) == H
    assert next_interval(base=12 * H, current=H, stop_reason="cap_hit", consecutive_empty=0) == MIN_INTERVAL_SECONDS


def test_a_walk_that_fits_doubles_the_interval_back_to_the_base():
    assert next_interval(base=12 * H, current=3 * H, stop_reason="known_page", consecutive_empty=0) == 6 * H
    assert next_interval(base=12 * H, current=9 * H, stop_reason="end_of_results", consecutive_empty=0) == 12 * H


def test_a_slice_that_keeps_coming_back_empty_is_walked_at_most_daily():
    assert next_interval(base=4 * H, current=4 * H, stop_reason="end_of_results",
                         consecutive_empty=3) == EMPTY_SLICE_INTERVAL_SECONDS
    assert next_interval(base=4 * H, current=4 * H, stop_reason="end_of_results",
                         consecutive_empty=2) == 4 * H


@pytest.mark.parametrize("reason", ["throttled_empty", "rate_limited", "error"])
def test_an_unfinished_walk_returns_in_30_minutes_and_keeps_the_interval(reason):
    assert next_interval(base=12 * H, current=5 * H, stop_reason=reason, consecutive_empty=0) == 5 * H
    assert next_due(NOW, 5 * H, reason) == NOW + timedelta(seconds=RETRY_SOON_SECONDS)


def test_a_finished_walk_is_due_after_its_interval():
    assert next_due(NOW, 4 * H, "end_of_results") == NOW + timedelta(hours=4)
