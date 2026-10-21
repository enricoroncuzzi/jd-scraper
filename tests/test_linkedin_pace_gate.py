"""The schedule gate: the closed window and the shared daily counter."""
from datetime import datetime, timedelta, timezone

import pytest

from src.linkedin.pace import (
    PaceBudget, PaceGateClosed, evaluate_gate,
)
from src.linkedin.schedule import ScheduleConfig, local_day

WED = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)  # Rome 14:00, weekday
NIGHT = datetime(2026, 10, 13, 23, 0, tzinfo=timezone.utc)  # Rome 01:00
WINDOWED = ScheduleConfig(closed_hours=(0, 6))


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
    budget = make_budget(fake, WINDOWED)
    gate = budget.gate()
    assert not gate.open and gate.reason == "closed_hours"
    with pytest.raises(PaceGateClosed) as excinfo:
        with budget.slot("search"):
            raise AssertionError("no request may run while the window is closed")
    assert excinfo.value.gate.reason == "closed_hours"
    assert _row(budget)[0] == 0 and _event_count(budget) == 0


def test_without_a_closed_window_the_gate_is_open_at_night(make_budget):
    fake = FakeTime(NIGHT)
    budget = make_budget(fake)
    gate = budget.gate()
    assert gate.open and gate.reason == "open"
    with budget.slot("search") as slot:
        slot.clean(200)
    assert _row(budget)[0] == 1 and _event_count(budget) == 1


# -- the shared daily counter -----------------------------------------------------------

def test_the_counter_never_closes_the_gate(make_budget):
    fake = FakeTime(WED)
    budget = make_budget(fake)
    budget.snapshot()  # create the row
    with budget.conn.cursor() as cur:
        cur.execute("UPDATE pace_budgets SET requests_today = 999999, day = %s "
                    "WHERE host = 'linkedin.com'", (local_day(ScheduleConfig(), WED),))
    gate = budget.gate()
    assert gate.open and gate.reason == "open"
    assert gate.requests_today == 999999 and gate.window == "weekday"
    assert not hasattr(gate, "cap")
    for kind in ("search", "description"):
        with budget.slot(kind) as slot:
            slot.clean(200)
    assert _row(budget)[0] == 1000001 and _event_count(budget) == 2


def test_the_counter_resets_on_the_next_rome_day(make_budget):
    fake = FakeTime(WED)
    budget = make_budget(fake)
    with budget.slot("search") as slot:
        slot.clean(200)
    assert budget.gate().requests_today == 1
    fake.now += timedelta(days=1)  # Thursday, Rome
    gate = budget.gate()
    assert gate.open and gate.requests_today == 0


def test_the_counter_resets_at_rome_midnight_with_the_gate_open(make_budget):
    fake = FakeTime(datetime(2026, 10, 14, 21, 58, tzinfo=timezone.utc))  # 23:58 Rome
    budget = make_budget(fake)
    with budget.slot("search") as slot:
        slot.clean(200)
    assert budget.gate().requests_today == 1
    fake.now += timedelta(minutes=10)  # 00:08 Rome, the next day
    gate = budget.gate()
    assert gate.open and gate.reason == "open" and gate.requests_today == 0


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
    assert gate.open and gate.reason == "open" and gate.requests_today == 5
    assert gate.window == "weekday" and not hasattr(gate, "cap")


# -- end to end through slot: restart, probes, the wait, logging failures ------------------

def test_a_request_granted_before_the_window_cannot_fire_inside_it(make_budget):
    # 21:58 UTC = 23:58 Rome and the pacer says the next slot is five minutes away.
    fake = FakeTime(datetime(2026, 10, 14, 21, 58, tzinfo=timezone.utc))
    budget = make_budget(fake, WINDOWED)
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


def test_a_failed_pace_events_insert_does_not_lose_the_count_or_the_refusal_wait(make_budget, capsys):
    fake = FakeTime(WED)
    budget = make_budget(fake)
    budget.snapshot()
    with budget.conn.cursor() as cur:
        cur.execute("DROP TABLE pace_events")
    with budget.slot("search") as slot:
        slot.throttled(429)  # the request works; only the telemetry row fails
    with budget.conn.cursor() as cur:
        cur.execute("SELECT requests_today, refusal_streak, cooldown_until IS NOT NULL "
                    "FROM pace_budgets WHERE host = 'linkedin.com'")
        assert cur.fetchone() == (1, 1, True)
    assert "could not record the search request" in capsys.readouterr().out


def test_two_services_share_one_counter_row(make_budget, store_conn):
    fake = FakeTime(datetime(2026, 10, 13, 22, 0, tzinfo=timezone.utc))  # Wed 00:00 Rome
    collector = make_budget(fake, service="collect-linkedin")
    processor = make_budget(fake, service="process")
    with collector.slot("search") as slot:
        slot.clean(200)
    with processor.slot("description") as slot:
        slot.clean(200)
    gate = collector.gate()
    assert gate.open and gate.requests_today == 2
    with collector.conn.cursor() as cur:
        cur.execute("SELECT service, count(*) FROM pace_events GROUP BY service ORDER BY service")
        assert cur.fetchall() == [("collect-linkedin", 1), ("process", 1)]
