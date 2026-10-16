import json
from datetime import date, datetime, timedelta, timezone

import pytest

from src.linkedin import pace as pace_module
from src.linkedin.pace import ManagedPace, PaceBudget
from src.linkedin.schedule import ScheduleConfig

WED_NOON = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)  # 12:00 Rome, Wednesday


class FakeTime:
    def __init__(self, now=WED_NOON):
        self.now = now
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
    def make(fake: FakeTime, **options) -> PaceBudget:
        conn = store_conn_factory(store_conn)
        conn.autocommit = True
        return PaceBudget(conn, clock=fake.clock, sleep=fake.sleep, uniform=lambda a, b: (a + b) / 2,
                          schedule=ScheduleConfig(), **options)
    return make


def _row(budget, *columns):
    with budget.conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(columns)} FROM pace_budgets WHERE host = 'linkedin.com'")
        return cur.fetchone()


def _set(budget, **columns):
    with budget.conn.cursor() as cur:
        cur.execute(f"UPDATE pace_budgets SET {', '.join(f'{name} = %s' for name in columns)} "
                    "WHERE host = 'linkedin.com'", list(columns.values()))


def _events(budget, *columns):
    with budget.conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(columns)} FROM pace_events ORDER BY id")
        return cur.fetchall()


def test_consecutive_requests_are_spaced_75_seconds(make_budget):
    fake = FakeTime()
    budget = make_budget(fake)
    with budget.slot("search") as slot:
        slot.clean(200)
    assert fake.sleeps == []
    with budget.slot("search") as slot:
        slot.clean(200)
    assert sum(fake.sleeps) == pytest.approx(75)


def test_refusals_wait_60_120_240_seconds_and_a_clean_response_resets(make_budget):
    fake = FakeTime()
    budget = make_budget(fake)
    for streak, wait in ((1, 60), (2, 120), (3, 240)):
        with budget.slot("search") as slot:
            slot.throttled(429)
            refused_at = fake.now
        assert _row(budget, "refusal_streak", "cooldown_until") == (streak, refused_at + timedelta(seconds=wait))
    with budget.slot("search") as slot:
        slot.clean(200)
        clean_at = fake.now
    assert _row(budget, "refusal_streak", "cooldown_until", "last_clean_at") == (0, None, clean_at)


def test_retry_after_from_the_response_sets_the_wait(make_budget):
    fake = FakeTime()
    budget = make_budget(fake)
    with budget.slot("search") as slot:
        slot.throttled(429, retry_after=300)
    assert _row(budget, "cooldown_until") == (WED_NOON + timedelta(seconds=300),)


def test_a_neutral_response_keeps_the_refusal_streak(make_budget):
    fake = FakeTime()
    budget = make_budget(fake)
    with budget.slot("search") as slot:
        slot.throttled(429)
    wait_until = _row(budget, "cooldown_until")[0]
    with budget.slot("search") as slot:
        slot.neutral(503)
    assert _row(budget, "refusal_streak", "cooldown_until") == (1, wait_until)


def test_a_refusal_never_closes_the_gate(make_budget):
    fake = FakeTime()
    budget = make_budget(fake)
    for _ in range(10):
        with budget.slot("search") as slot:
            slot.throttled(429)
    gate = budget.gate()
    assert gate.open and gate.reason == "open"


def test_a_refusal_wait_survives_a_restart(make_budget):
    fake = FakeTime()
    before = make_budget(fake)
    with before.slot("search") as slot:
        slot.throttled(429, retry_after=300)  # wait_until = refusal + 300 s, beyond the spacing
    refused_at = fake.now
    before.conn.close()                         # the service restarts: nothing survives in memory
    after = make_budget(fake)
    with after.slot("search") as slot:
        slot.clean(200)
    assert sum(fake.sleeps) == pytest.approx(300)
    assert fake.now == refused_at + timedelta(seconds=300)


def test_the_day_counts_requests_and_refusals(make_budget):
    fake = FakeTime()
    budget = make_budget(fake)
    with budget.slot("search") as slot:
        slot.clean(200)
    with budget.slot("search") as slot:
        slot.throttled(429)
    with budget.slot("search") as slot:
        slot.neutral(503)
    assert _row(budget, "requests_today", "refusals_today") == (3, 1)


def test_every_request_logs_its_spacing_and_its_refusal_wait(make_budget):
    fake = FakeTime()
    budget = make_budget(fake)
    with budget.slot("search") as slot:
        slot.clean(200)
    with budget.slot("search") as slot:
        slot.throttled(429)
    # the second spacing carries the hour nudge: 1 refusal in 2 requests is over 10%
    assert _events(budget, "outcome", "spacing_seconds", "wait_seconds", "factor") == [
        ("ok", 75.0, None, 1.0), ("refused", 112.5, 60.0, 1.5)]


def test_a_stale_breaker_rest_from_the_old_code_does_not_close_the_gate(make_budget):
    fake = FakeTime()
    budget = make_budget(fake)
    budget.snapshot()
    _set(budget, rest_until=fake.now + timedelta(hours=6), rest_level=2,
         refusal_times=[fake.now - timedelta(hours=20)], next_slot_at=fake.now)
    gate = budget.gate()
    assert gate.open and gate.reason == "open"
    with budget.slot("search") as slot:
        slot.clean(200)
    assert fake.sleeps == [] and _row(budget, "nudge") == (1.0,)


def test_the_old_aimd_factor_is_ignored(make_budget):
    fake = FakeTime()
    budget = make_budget(fake)
    with budget.slot("search") as slot:
        slot.clean(200)
    _set(budget, factor=100.0)
    with budget.slot("search") as slot:
        slot.clean(200)
    assert sum(fake.sleeps) == pytest.approx(75)  # not 7,500 s


def test_an_exception_inside_the_slot_releases_the_lock_and_changes_no_rate(make_budget):
    fake = FakeTime()
    budget = make_budget(fake)
    with pytest.raises(RuntimeError):
        with budget.slot("search"):
            raise RuntimeError("network died")
    assert _row(budget, "refusal_streak", "cooldown_until", "requests_today") == (0, None, 1)
    with budget.slot("search") as slot:  # lock was released
        slot.clean(200)


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
        slot.clean(200)
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
        slot.clean(200)
    assert pace_module.YIELD_SECONDS in fake.sleeps
    assert _row(budget, "last_kind")[0] == "search"


def test_a_stale_waiting_marker_does_not_block_the_same_kind(make_budget):
    fake = FakeTime()
    budget = make_budget(fake)
    with budget.slot("search") as slot:
        slot.clean(200)
    with budget.conn.cursor() as cur:
        cur.execute("UPDATE pace_budgets SET wanting = %s::jsonb WHERE host = 'linkedin.com'",
                    (json.dumps({"description": fake.clock().timestamp() - 600}),))
    with budget.slot("search") as slot:
        slot.clean(200)
    assert pace_module.YIELD_SECONDS not in fake.sleeps


def test_a_marker_declared_while_a_slot_is_held_survives_its_release(make_budget):
    fake = FakeTime()
    search, other = make_budget(fake), make_budget(fake)
    with search.slot("search") as slot:
        other._declare_wanting("description")
        assert "description" in search.snapshot().wanting
    assert "description" in search.snapshot().wanting
    before = len(fake.sleeps)
    with search.slot("search") as slot:
        slot.clean(200)
    assert pace_module.YIELD_SECONDS in fake.sleeps[before:]


def test_managed_pace_reopens_a_dropped_connection(store_conn, store_conn_factory):
    opened = []

    def connect():
        conn = store_conn_factory(store_conn)
        opened.append(conn)
        return conn

    fake = FakeTime()
    managed = ManagedPace(connect, clock=fake.clock, sleep=fake.sleep, uniform=lambda a, b: (a + b) / 2)
    with managed.slot("search") as slot:
        slot.clean(200)
    opened[0].close()
    with managed.slot("search") as slot:
        slot.clean(200)
    assert len(opened) == 2 and opened[0].closed and not opened[1].closed
    opened[1].close()


def test_the_day_verdict_is_persisted_and_tightens_the_spacing(make_budget):
    fake = FakeTime()
    budget = make_budget(fake)
    budget.snapshot()
    _set(budget, day=date(2026, 10, 12), requests_today=500, refusals_today=0, calm_days=2)
    with budget.slot("search") as slot:
        slot.clean(200)
    assert _row(budget, "weekday_spacing_seconds")[0] == pytest.approx(67.5)
    assert _row(budget, "calm_days")[0] == 0
