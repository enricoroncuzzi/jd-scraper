"""The schedule gate: quiet hours, the shared daily cap and the circuit breaker."""
from datetime import datetime, timedelta, timezone

import pytest

from src.linkedin.pace import (
    PaceBudget, PaceGateClosed, PaceState, evaluate_gate, update_breaker,
)
from src.linkedin.schedule import ScheduleConfig

WED = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)  # Rome 14:00, weekday
NIGHT = datetime(2026, 10, 13, 23, 0, tzinfo=timezone.utc)  # Rome 01:00
SAT = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


class FakeTime:
    def __init__(self, now=WED):
        self.now = now

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def make_budget(store_conn, store_conn_factory):
    def make(fake, schedule=None) -> PaceBudget:
        conn = store_conn_factory(store_conn)
        conn.autocommit = True
        return PaceBudget(conn, clock=fake.clock, sleep=fake.sleep, uniform=lambda a, b: a,
                          service="collect-linkedin", schedule=schedule or ScheduleConfig())
    return make


def _row(budget):
    with budget.conn.cursor() as cur:
        cur.execute("SELECT requests_today, day, rest_until, rest_level, refusal_times "
                    "FROM pace_budgets WHERE host = 'linkedin.com'")
        return cur.fetchone()


def _event_count(budget) -> int:
    with budget.conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pace_events")
        return cur.fetchone()[0]


# -- the quiet window -------------------------------------------------------------------

def test_the_quiet_window_closes_the_gate_and_the_slot_makes_no_request(make_budget):
    fake = FakeTime(NIGHT)
    budget = make_budget(fake)
    gate = budget.gate()
    assert not gate.open and gate.reason == "closed_hours"
    with pytest.raises(PaceGateClosed) as excinfo:
        with budget.slot("search"):
            raise AssertionError("no request may run while the window is closed")
    assert excinfo.value.gate.reason == "closed_hours"
    assert _row(budget)[0] == 0 and _event_count(budget) == 0


# -- the shared daily cap ---------------------------------------------------------------

def test_search_and_descriptions_share_one_cap_and_it_blocks_at_the_limit(make_budget):
    fake = FakeTime(WED)
    budget = make_budget(fake, ScheduleConfig(weekday_cap=3, breaker_refusals=1000))
    for kind in ("search", "description", "search"):
        with budget.slot(kind) as slot:
            slot.clean(200)
    gate = budget.gate()
    assert not gate.open and gate.reason == "daily_cap"
    assert gate.requests_today == 3 and gate.cap == 3 and gate.window == "weekday"
    with pytest.raises(PaceGateClosed):
        with budget.slot("description") as slot:
            slot.clean(200)
    assert _row(budget)[0] == 3


def test_the_weekend_uses_the_smaller_cap(make_budget):
    fake = FakeTime(SAT)
    budget = make_budget(fake, ScheduleConfig(weekend_cap=2, breaker_refusals=1000))
    for _ in range(2):
        with budget.slot("search") as slot:
            slot.clean(200)
    gate = budget.gate()
    assert gate.window == "weekend" and gate.cap == 2
    assert not gate.open and gate.reason == "daily_cap"


def test_the_counter_resets_on_the_next_rome_day(make_budget):
    fake = FakeTime(WED)
    budget = make_budget(fake, ScheduleConfig(weekday_cap=1, breaker_refusals=1000))
    with budget.slot("search") as slot:
        slot.clean(200)
    assert budget.gate().reason == "daily_cap"
    fake.now += timedelta(days=1)  # Thursday, Rome
    gate = budget.gate()
    assert gate.open and gate.requests_today == 0


# -- the circuit breaker -----------------------------------------------------------------

def test_the_breaker_ladder_escalates_two_six_twentyfour_and_repeats():
    schedule = ScheduleConfig()
    state = PaceState(next_slot_at=WED)
    for _ in range(3):
        update_breaker(state, "throttled", WED, schedule)
    assert state.rest_level == 1 and state.rest_until == WED + timedelta(hours=2)
    first_rest_end = state.rest_until
    update_breaker(state, "throttled", first_rest_end, schedule)
    assert state.rest_level == 2 and state.rest_until == first_rest_end + timedelta(hours=6)
    second_rest_end = state.rest_until
    update_breaker(state, "throttled", second_rest_end, schedule)
    assert state.rest_level == 3 and state.rest_until == second_rest_end + timedelta(hours=24)
    third_rest_end = state.rest_until
    update_breaker(state, "throttled", third_rest_end, schedule)
    assert state.rest_level == 4 and state.rest_until == third_rest_end + timedelta(hours=24)


def test_a_clean_probe_resets_the_ladder():
    schedule = ScheduleConfig()
    state = PaceState(next_slot_at=WED)
    for _ in range(3):
        update_breaker(state, "throttled", WED, schedule)
    update_breaker(state, "clean", state.rest_until + timedelta(seconds=1), schedule)
    assert state.rest_level == 0 and state.rest_until is None and state.refusal_times == []


def test_a_repeat_refusal_of_the_same_logical_request_does_not_count():
    schedule = ScheduleConfig()
    state = PaceState(next_slot_at=WED)
    for _ in range(2):
        update_breaker(state, "throttled", WED, schedule)
    for _ in range(5):
        update_breaker(state, "throttled_again", WED, schedule)
    assert state.rest_level == 0 and state.rest_until is None
    update_breaker(state, "throttled", WED, schedule)
    assert state.rest_level == 1


def test_refusals_outside_the_thirty_minute_window_are_forgotten():
    schedule = ScheduleConfig()
    state = PaceState(next_slot_at=WED)
    update_breaker(state, "throttled", WED, schedule)
    update_breaker(state, "throttled", WED + timedelta(minutes=31), schedule)
    assert state.refusal_times == [WED + timedelta(minutes=31)]
    assert state.rest_level == 0


def test_a_breaker_rest_closes_the_gate(make_budget):
    fake = FakeTime(WED)
    budget = make_budget(fake)
    budget.snapshot()  # creates the row
    with budget.conn.cursor() as cur:
        cur.execute("UPDATE pace_budgets SET rest_until = %s, rest_level = 1 WHERE host = 'linkedin.com'",
                    (WED + timedelta(hours=2),))
    gate = budget.gate()
    assert not gate.open and gate.reason == "breaker"
    assert gate.rest_until == WED + timedelta(hours=2)
    with pytest.raises(PaceGateClosed):
        with budget.slot("search"):
            pass


def test_every_request_is_logged_with_service_kind_outcome_and_status(make_budget):
    fake = FakeTime(WED)
    budget = make_budget(fake)
    with budget.slot("search") as slot:
        slot.clean(200)
    with budget.slot("description") as slot:
        slot.throttled(429)
    with budget.conn.cursor() as cur:
        cur.execute("SELECT service, kind, outcome, status, requests_today FROM pace_events ORDER BY id")
        rows = cur.fetchall()
    assert rows == [("collect-linkedin", "search", "ok", 200, 1),
                    ("collect-linkedin", "description", "refused", 429, 2)]


def test_evaluate_gate_reports_open_when_everything_is_fine():
    gate = evaluate_gate(ScheduleConfig(), WED, requests_today=5)
    assert gate.open and gate.reason == "open" and gate.requests_today == 5 and gate.cap == 1400
