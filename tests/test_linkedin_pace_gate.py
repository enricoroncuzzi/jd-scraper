"""The schedule gate: quiet hours and the shared daily cap."""
from datetime import datetime, timedelta, timezone

import pytest

from src.linkedin.pace import (
    PaceBudget, PaceGateClosed, evaluate_gate,
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
    def make(fake, schedule=None, service="collect-linkedin") -> PaceBudget:
        conn = store_conn_factory(store_conn)
        conn.autocommit = True
        return PaceBudget(conn, clock=fake.clock, sleep=fake.sleep, uniform=lambda a, b: a,
                          service=service, schedule=schedule or ScheduleConfig())
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
    budget = make_budget(fake, ScheduleConfig(weekday_cap=3))
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
    budget = make_budget(fake, ScheduleConfig(weekend_cap=2))
    for _ in range(2):
        with budget.slot("search") as slot:
            slot.clean(200)
    gate = budget.gate()
    assert gate.window == "weekend" and gate.cap == 2
    assert not gate.open and gate.reason == "daily_cap"


def test_the_counter_resets_on_the_next_rome_day(make_budget):
    fake = FakeTime(WED)
    budget = make_budget(fake, ScheduleConfig(weekday_cap=1))
    with budget.slot("search") as slot:
        slot.clean(200)
    assert budget.gate().reason == "daily_cap"
    fake.now += timedelta(days=1)  # Thursday, Rome
    gate = budget.gate()
    assert gate.open and gate.requests_today == 0


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


# -- end to end through slot: restart, probes, the wait, logging failures ------------------

def test_a_request_granted_before_the_window_cannot_fire_inside_it(make_budget):
    # 21:58 UTC = 23:58 Rome and the pacer says the next slot is five minutes away.
    fake = FakeTime(datetime(2026, 10, 14, 21, 58, tzinfo=timezone.utc))
    budget = make_budget(fake)
    budget.snapshot()
    with budget.conn.cursor() as cur:
        cur.execute("UPDATE pace_budgets SET next_slot_at = %s WHERE host = 'linkedin.com'",
                    (fake.now + timedelta(minutes=5),))
    ran = []
    with pytest.raises(PaceGateClosed):
        with budget.slot("search") as slot:
            ran.append(fake.now)
            slot.clean(200)
    assert ran == [] and _row(budget)[0] == 0


def test_a_failed_pace_events_insert_does_not_lose_the_count_or_the_breaker(make_budget, capsys):
    fake = FakeTime(WED)
    budget = make_budget(fake)
    budget.snapshot()
    with budget.conn.cursor() as cur:
        cur.execute("DROP TABLE pace_events")
    with budget.slot("search") as slot:
        slot.throttled(429)  # the request works; only the telemetry row fails
    with budget.conn.cursor() as cur:
        cur.execute("SELECT requests_today, factor FROM pace_budgets WHERE host = 'linkedin.com'")
        assert cur.fetchone() == (1, 2.0)
    assert "could not record the search request" in capsys.readouterr().out


def test_two_services_share_one_cap_row(make_budget, store_conn):
    fake = FakeTime(WED)
    schedule = ScheduleConfig(weekday_cap=2)
    collector = make_budget(fake, schedule, service="collect-linkedin")
    processor = make_budget(fake, schedule, service="process")
    with collector.slot("search") as slot:
        slot.clean(200)
    with processor.slot("description") as slot:
        slot.clean(200)
    assert collector.gate().requests_today == 2 and not collector.gate().open
    with pytest.raises(PaceGateClosed):
        with processor.slot("description") as slot:
            slot.clean(200)
    with collector.conn.cursor() as cur:
        cur.execute("SELECT service, count(*) FROM pace_events GROUP BY service ORDER BY service")
        assert cur.fetchall() == [("collect-linkedin", 1), ("process", 1)]
