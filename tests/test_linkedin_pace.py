import json
from datetime import datetime, timedelta, timezone

import pytest

from src.linkedin import pace as pace_module
from src.linkedin.pace import (
    CLEAN_STREAK_FOR_RAISE, COOLDOWN_SECONDS, FLOOR_SPACING_SECONDS, MAX_FACTOR, PaceBudget,
    PaceState, after_calm, after_clean, after_repeat_throttle, after_throttle, spacing_seconds,
)
from src.linkedin.schedule import ScheduleConfig

T0 = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)


def _state(**overrides) -> PaceState:
    return PaceState(next_slot_at=T0, **overrides)


def test_a_throttle_halves_the_rate_and_starts_a_cooldown():
    state = after_throttle(_state(clean_streak=30), T0)
    assert state.factor == 2.0 and state.clean_streak == 0
    assert state.cooldown_until == T0 + timedelta(seconds=COOLDOWN_SECONDS)


def test_repeated_throttles_stop_at_the_floor():
    state = _state()
    for _ in range(20):
        after_throttle(state, T0)
    assert state.factor == MAX_FACTOR
    assert spacing_seconds(state.factor, uniform=lambda a, b: b) == FLOOR_SPACING_SECONDS


def test_fifty_clean_responses_raise_the_rate_ten_percent_up_to_the_base():
    state = _state(factor=2.0)
    for _ in range(CLEAN_STREAK_FOR_RAISE - 1):
        after_clean(state)
    assert state.factor == 2.0
    after_clean(state)
    assert state.factor == pytest.approx(2.0 / 1.10)
    assert state.clean_streak == 0
    base = _state(factor=1.0)
    for _ in range(CLEAN_STREAK_FOR_RAISE * 3):
        after_clean(base)
    assert base.factor == 1.0


def test_base_spacing_is_two_to_four_seconds():
    assert spacing_seconds(1.0, uniform=lambda a, b: a) == 2.0
    assert spacing_seconds(1.0, uniform=lambda a, b: b) == 4.0


class FakeTime:
    def __init__(self):
        self.now = T0
        self.sleeps = []
        self.on_sleep = None

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)
        if self.on_sleep:
            self.on_sleep(seconds)


@pytest.fixture
def make_budget(store_conn, store_conn_factory):
    def make(fake: FakeTime) -> PaceBudget:
        conn = store_conn_factory(store_conn)
        conn.autocommit = True
        # These tests exercise the AIMD rate only; the circuit breaker has its own tests.
        return PaceBudget(conn, clock=fake.clock, sleep=fake.sleep, uniform=lambda a, b: a,
                          schedule=ScheduleConfig(breaker_refusals=1000))
    return make


def _row(budget):
    with budget.conn.cursor() as cur:
        cur.execute("SELECT factor, clean_streak, last_kind, cooldown_until, next_slot_at "
                    "FROM pace_budgets WHERE host = 'linkedin.com'")
        return cur.fetchone()


def test_consecutive_requests_are_spaced(make_budget):
    fake = FakeTime()
    budget = make_budget(fake)
    with budget.slot("search") as slot:
        slot.clean()
    assert fake.sleeps == []
    with budget.slot("search") as slot:
        slot.clean()
    assert fake.sleeps == [pytest.approx(2.0)]


def test_a_throttle_persists_the_slower_rate_and_the_next_slot_waits_out_the_cooldown(make_budget):
    fake = FakeTime()
    budget = make_budget(fake)
    with budget.slot("search") as slot:
        slot.throttled()
    factor, streak, last_kind, cooldown, _next = _row(budget)
    assert factor == 2.0 and streak == 0 and last_kind == "search"
    assert cooldown == T0 + timedelta(seconds=COOLDOWN_SECONDS)
    with budget.slot("search") as slot:
        slot.clean()
    assert fake.sleeps[-1] == pytest.approx(COOLDOWN_SECONDS)


def test_an_exception_inside_the_slot_releases_the_lock_and_changes_no_rate(make_budget):
    fake = FakeTime()
    budget = make_budget(fake)
    with pytest.raises(RuntimeError):
        with budget.slot("search"):
            raise RuntimeError("network died")
    assert _row(budget)[0] == 1.0
    with budget.slot("search") as slot:  # lock was released
        slot.clean()


def test_no_second_request_can_be_in_flight(make_budget):
    fake = FakeTime()
    first, second = make_budget(fake), make_budget(fake)
    with first.slot("search"):
        assert second._try_lock() is False
    assert second._try_lock() is True
    second._unlock()


def test_a_kind_yields_to_the_other_kind_that_is_waiting(make_budget):
    fake = FakeTime()
    budget = make_budget(fake)
    other = make_budget(fake)
    with budget.slot("search") as slot:
        slot.clean()
    with other.conn.cursor() as cur:
        cur.execute("UPDATE pace_budgets SET wanting = %s::jsonb WHERE host = 'linkedin.com'",
                    (json.dumps({"description": fake.clock().timestamp()}),))

    def description_takes_its_turn(_seconds):
        if _seconds == pace_module.YIELD_SECONDS:
            with other.conn.cursor() as cur:
                cur.execute("UPDATE pace_budgets SET last_kind = 'description', wanting = '{}'::jsonb "
                            "WHERE host = 'linkedin.com'")
    fake.on_sleep = description_takes_its_turn

    with budget.slot("search") as slot:
        slot.clean()
    assert pace_module.YIELD_SECONDS in fake.sleeps
    assert _row(budget)[2] == "search"


def test_a_stale_waiting_marker_does_not_block_the_same_kind(make_budget):
    fake = FakeTime()
    budget = make_budget(fake)
    with budget.slot("search") as slot:
        slot.clean()
    with budget.conn.cursor() as cur:
        cur.execute("UPDATE pace_budgets SET wanting = %s::jsonb WHERE host = 'linkedin.com'",
                    (json.dumps({"description": fake.clock().timestamp() - 600}),))
    with budget.slot("search") as slot:
        slot.clean()
    assert pace_module.YIELD_SECONDS not in fake.sleeps


def test_a_marker_declared_while_a_slot_is_held_survives_its_release(make_budget):
    fake = FakeTime()
    search = make_budget(fake)
    other = make_budget(fake)
    with search.slot("search") as slot:
        slot.clean()
        # the description caller arrives while the search request is in flight
        other._declare_wanting("description")
        assert "description" in search.snapshot().wanting
    assert "description" in search.snapshot().wanting
    # and the search caller immediately comes back, so it must yield a turn
    before = len(fake.sleeps)
    with search.slot("search") as slot:
        slot.clean()
    assert pace_module.YIELD_SECONDS in fake.sleeps[before:]


def test_managed_pace_reopens_a_dropped_connection(store_conn, store_conn_factory):
    from src.linkedin.pace import ManagedPace

    opened = []

    def connect():
        conn = store_conn_factory(store_conn)
        opened.append(conn)
        return conn

    fake = FakeTime()
    managed = ManagedPace(connect, clock=fake.clock, sleep=fake.sleep, uniform=lambda a, b: a)
    with managed.slot("search") as slot:
        slot.clean()
    opened[0].close()
    with managed.slot("search") as slot:
        slot.clean()
    assert len(opened) == 2 and opened[0].closed and not opened[1].closed
    opened[1].close()


def test_a_throttle_is_logged_with_the_new_spacing(make_budget, capsys):
    budget = make_budget(FakeTime())
    with budget.slot("description") as slot:
        slot.throttled()
    assert "LinkedIn throttled a description request: spacing now x2, cooling down 60s" in capsys.readouterr().out
    with budget.slot("search") as slot:
        slot.clean()
    assert "throttled" not in capsys.readouterr().out


# -- one throttle per logical request, and recovery by time ----------------------------------

def test_a_repeat_refusal_keeps_the_cooldown_but_does_not_double_again():
    state = after_throttle(_state(clean_streak=9), T0)
    later = T0 + timedelta(seconds=30)
    after_repeat_throttle(state, later)
    assert state.factor == 2.0 and state.clean_streak == 0
    assert state.cooldown_until == later + timedelta(seconds=COOLDOWN_SECONDS)
    assert state.calm_since == later


def test_every_full_half_hour_without_a_refusal_halves_the_spacing():
    state = _state(factor=32.0, calm_since=T0)
    after_calm(state, T0 + timedelta(minutes=29, seconds=59))
    assert state.factor == 32.0
    after_calm(state, T0 + timedelta(minutes=30))
    assert state.factor == 16.0 and state.calm_since == T0 + timedelta(minutes=30)
    after_calm(state, T0 + timedelta(minutes=95))  # two more full periods
    assert state.factor == 4.0 and state.calm_since == T0 + timedelta(minutes=90)


def test_calm_never_takes_the_spacing_below_the_base():
    state = _state(factor=3.0, calm_since=T0)
    after_calm(state, T0 + timedelta(hours=10))
    assert state.factor == 1.0


def test_the_calm_clock_starts_when_it_is_first_seen():
    state = _state(factor=64.0)
    after_calm(state, T0)
    assert state.factor == 64.0 and state.calm_since == T0


def test_a_throttle_restarts_the_calm_clock():
    state = _state(factor=4.0, calm_since=T0)
    after_throttle(state, T0 + timedelta(minutes=29))
    assert state.factor == 8.0 and state.calm_since == T0 + timedelta(minutes=29)
    after_calm(state, T0 + timedelta(minutes=58))
    assert state.factor == 8.0


def _factor_and_calm(budget):
    with budget.conn.cursor() as cur:
        cur.execute("SELECT factor, calm_since FROM pace_budgets WHERE host = 'linkedin.com'")
        return cur.fetchone()


def test_a_refused_retry_through_the_budget_doubles_only_once(make_budget):
    budget = make_budget(FakeTime())
    with budget.slot("description") as slot:
        slot.throttled()
    for _ in range(3):
        with budget.slot("description") as slot:
            slot.throttled_again()
    assert _factor_and_calm(budget)[0] == 2.0


def test_the_stored_spacing_halves_after_half_an_hour_of_calm_and_persists(make_budget):
    fake = FakeTime()
    budget = make_budget(fake)
    for _ in range(3):  # three separate requests refused: x8
        with budget.slot("search") as slot:
            slot.throttled()
    factor, last_refusal = _factor_and_calm(budget)
    assert factor == 8.0
    fake.now = last_refusal + timedelta(minutes=31)
    with budget.slot("search") as slot:
        slot.clean()
    factor, calm_since = _factor_and_calm(budget)
    assert factor == 4.0 and calm_since == last_refusal + timedelta(minutes=30)
    assert budget.snapshot().factor == 4.0


def test_a_new_refusal_restarts_the_half_hour_clock(make_budget):
    fake = FakeTime()
    budget = make_budget(fake)
    for _ in range(2):
        with budget.slot("search") as slot:
            slot.throttled()
    fake.now += timedelta(minutes=29)
    with budget.slot("search") as slot:
        slot.throttled()
    assert _factor_and_calm(budget)[0] == 8.0
    fake.now += timedelta(minutes=29)
    with budget.slot("search") as slot:
        slot.clean()
    assert _factor_and_calm(budget)[0] == 8.0


def test_the_snapshot_shows_the_decayed_spacing_without_writing_it(make_budget):
    fake = FakeTime()
    budget = make_budget(fake)
    with budget.slot("search") as slot:
        slot.throttled()
    fake.now += timedelta(minutes=61)
    assert budget.snapshot().factor == 1.0
    assert _factor_and_calm(budget)[0] == 2.0
