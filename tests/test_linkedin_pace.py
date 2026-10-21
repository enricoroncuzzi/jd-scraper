from datetime import date, datetime, timedelta, timezone

import pytest
from psycopg2.extras import Json

from src.linkedin import pace as pace_module
from src.linkedin.pace import ManagedPace, PaceBudget, PaceBusy, PaceGateClosed
from src.linkedin.schedule import ScheduleConfig

WED_NOON = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)  # 12:00 Rome, Wednesday
WINDOWED = ScheduleConfig(closed_hours=(0, 6))


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
    def make(fake: FakeTime, schedule: ScheduleConfig | None = None, **options) -> PaceBudget:
        conn = store_conn_factory(store_conn)
        conn.autocommit = True
        return PaceBudget(conn, clock=fake.clock, sleep=fake.sleep, uniform=lambda a, b: (a + b) / 2,
                          schedule=schedule or ScheduleConfig(), **options)
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


def test_consecutive_requests_are_spaced_75_seconds_in_chunks(make_budget):
    fake = FakeTime()
    budget = make_budget(fake)
    with budget.slot("search") as slot:
        slot.clean(200)
    with budget.slot("search") as slot:
        slot.clean(200)
    assert fake.sleeps == [pace_module.WAIT_CHUNK_SECONDS] * 5


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
    # The fixed pace carries on after a refusal: both spacings are the same 75 s.
    assert _events(budget, "outcome", "spacing_seconds", "wait_seconds") == [
        ("ok", 75.0, None), ("refused", 75.0, 60.0)]


def test_a_stale_live_row_base_and_nudge_do_not_slow_the_fixed_pace(make_budget):
    # The live pace_budgets row still stores weekday 99 s / weekend 133 s and nudge x3 from the
    # old adaptive code; the deploy must run at 75/110 x1 with no manual step.
    fake = FakeTime()
    budget = make_budget(fake)
    budget.snapshot()
    _set(budget, weekday_spacing_seconds=99.0, weekend_spacing_seconds=133.0, nudge=3.0)
    with budget.slot("search") as slot:
        slot.clean(200)
    assert _row(budget, "next_slot_at") == (WED_NOON + timedelta(seconds=75),)


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
    assert fake.sleeps == [] and _row(budget, "requests_today") == (1,)


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


def test_an_overdue_description_keeps_a_turn_after_a_search_within_the_processor_budget(make_budget):
    # Two spacings exceed the 120 s budget. The description yields because its run is at the
    # cap; the search takes its turn; the description is then next in line and must not
    # give up on elapsed time, only on a remaining wait longer than the budget.
    fake = FakeTime()
    description, search = make_budget(fake, give_up_after=120), make_budget(fake)
    description.snapshot()
    _set(description, description_run=2, last_kind="description",
         next_slot_at=fake.now + timedelta(seconds=75))
    search._declare_wanting("search")
    taken = []

    def the_search_walks(_seconds):
        search._declare_wanting("search")
        if not taken and fake.now >= WED_NOON + timedelta(seconds=75):
            saved, fake.on_sleep = fake.on_sleep, None
            with search.slot("search") as slot:
                taken.append(("search", fake.now))
                slot.clean(200)
            fake.on_sleep = saved
    fake.on_sleep = the_search_walks
    with description.slot("description", overdue=True) as slot:
        taken.append(("description", fake.now))
        slot.clean(200)
    assert [k for k, _ in taken] == ["search", "description"]
    assert taken[1][1] == WED_NOON + timedelta(seconds=150)


def test_the_lock_is_not_held_while_waiting(make_budget):
    fake = FakeTime()
    budget, other = make_budget(fake), make_budget(fake)
    with budget.slot("search") as slot:
        assert other._try_lock() is False   # held for the request itself
        slot.clean(200)
    free = []

    def other_tries_the_lock(_seconds):
        got = other._try_lock()
        free.append(got)
        if got:
            other._unlock()
    fake.on_sleep = other_tries_the_lock
    with budget.slot("search") as slot:     # waits 75 s: five 15 s chunks
        slot.clean(200)
    assert free == [True] * 5               # the old loop slept once, locked: [False]


def test_a_description_takes_its_turn_over_a_waiting_search(make_budget):
    fake = FakeTime()
    description, search = make_budget(fake), make_budget(fake)
    description.snapshot()                  # the row exists and the next slot is now
    search._declare_wanting("search")        # a walk is waiting for its turn
    with description.slot("description") as slot:
        slot.clean(200)
    assert fake.sleeps == []                 # the share gives this turn to the description
    assert _events(description, "kind") == [("description",)]
    assert _row(description, "last_kind", "description_run") == ("description", 1)


def test_a_search_yields_while_descriptions_are_under_their_share(make_budget):
    fake = FakeTime()
    search, description = make_budget(fake), make_budget(fake)
    search.snapshot()
    description._declare_wanting("description")
    order = []

    def the_description_takes_its_turn(seconds):
        if seconds == pace_module.YIELD_SECONDS and not order:
            fake.on_sleep = None
            with description.slot("description") as slot:
                order.append("description")
                slot.clean(200)
    fake.on_sleep = the_description_takes_its_turn
    with search.slot("search") as slot:
        order.append("search")
        slot.clean(200)
    assert order == ["description", "search"]
    assert fake.sleeps[0] == pace_module.YIELD_SECONDS
    assert sum(fake.sleeps[1:]) == pytest.approx(75)   # the search waits out the description's spacing
    assert _events(description, "kind") == [("description",), ("search",)]


def test_a_description_yields_the_turn_to_a_waiting_packaging_request(make_budget):
    # The packaging caller asks for one LinkedIn turn per package, so it is granted the
    # next turn: a describer waiting for that same turn must give way to it.
    fake = FakeTime()
    packaging, describer = make_budget(fake), make_budget(fake)
    packaging.snapshot()
    packaging._declare_wanting("packaging")
    order = []

    def the_packaging_request_takes_its_turn(seconds):
        if seconds == pace_module.YIELD_SECONDS and not order:
            fake.on_sleep = None
            with packaging.slot("packaging") as slot:
                order.append("packaging")
                slot.clean(200)
            fake.on_sleep = the_packaging_request_takes_its_turn
    fake.on_sleep = the_packaging_request_takes_its_turn
    with describer.slot("description") as slot:
        order.append("description")
        slot.clean(200)
    assert order == ["packaging", "description"]
    assert fake.sleeps[0] == pace_module.YIELD_SECONDS
    assert sum(fake.sleeps[1:]) == pytest.approx(75)   # it waits out the packaging spacing
    assert _events(packaging, "kind") == [("packaging",), ("description",)]


def test_a_search_yields_the_turn_to_a_waiting_packaging_request(make_budget):
    # The share would have given this turn to the search (the description run is at the
    # cap), but a waiting packaging request outranks both steady kinds.
    fake = FakeTime()
    search, packaging = make_budget(fake), make_budget(fake)
    search.snapshot()
    _set(search, description_run=2, last_kind="description")
    packaging._declare_wanting("packaging")
    order = []

    def the_packaging_request_takes_its_turn(seconds):
        if seconds == pace_module.YIELD_SECONDS and not order:
            fake.on_sleep = None
            with packaging.slot("packaging") as slot:
                order.append("packaging")
                slot.clean(200)
            fake.on_sleep = the_packaging_request_takes_its_turn
    fake.on_sleep = the_packaging_request_takes_its_turn
    with search.slot("search") as slot:
        order.append("search")
        slot.clean(200)
    assert order == ["packaging", "search"]
    assert fake.sleeps[0] == pace_module.YIELD_SECONDS
    assert _events(packaging, "kind") == [("packaging",), ("search",)]


def test_a_packaging_turn_keeps_the_description_run_where_it_was(make_budget):
    # A packaging turn is neither a description turn nor a search turn, so it must not
    # reset the two-of-three description run (nor count towards it).
    fake = FakeTime()
    budget = make_budget(fake)
    budget.snapshot()
    _set(budget, description_run=1, last_kind="description")
    with budget.slot("packaging") as slot:
        slot.clean(200)
    assert _row(budget, "last_kind", "description_run") == ("packaging", 1)


def test_a_stale_packaging_marker_does_not_hold_back_a_description(make_budget):
    fake = FakeTime()
    budget = make_budget(fake)
    budget.snapshot()
    _set(budget, wanting=Json({"packaging": fake.now.timestamp() - 600}))
    with budget.slot("description") as slot:
        slot.clean(200)
    assert fake.sleeps == []


def test_a_search_takes_its_turn_once_the_description_run_reaches_the_cap(make_budget):
    fake = FakeTime()
    search, description = make_budget(fake), make_budget(fake)
    search.snapshot()
    _set(search, description_run=2, last_kind="description")
    description._declare_wanting("description")
    with search.slot("search") as slot:
        slot.clean(200)
    assert fake.sleeps == []
    assert _row(search, "last_kind", "description_run") == ("search", 0)


def test_an_overdue_description_takes_the_turn_before_a_search(make_budget):
    fake = FakeTime()
    description, search = make_budget(fake), make_budget(fake)
    description.snapshot()
    search._declare_wanting("search")
    with description.slot("description", overdue=True) as slot:
        slot.clean(200)
    assert fake.sleeps == []
    assert _row(description, "last_kind", "description_run") == ("description", 1)


def test_the_description_marker_is_dropped_after_its_turn(make_budget):
    fake = FakeTime()
    budget = make_budget(fake)
    with budget.slot("description") as slot:
        assert "description" in _row(budget, "wanting")[0]
        slot.clean(200)
    assert _row(budget, "wanting") == ({},)


def test_two_waiters_wake_together_and_the_description_goes_first(make_budget):
    fake = FakeTime()
    description, search = make_budget(fake), make_budget(fake)
    description.snapshot()
    _set(description, next_slot_at=fake.now + timedelta(seconds=10))
    description._declare_wanting("description")
    search._declare_wanting("search")          # the search caller sleeps in its own 10 s wait
    order = []

    def hook(seconds):
        if seconds == 10:                      # both waits end: the search caller wakes and re-declares
            search._declare_wanting("search")
        elif seconds == pace_module.YIELD_SECONDS and not order:
            fake.on_sleep = None
            with description.slot("description") as slot:
                order.append("description")
                slot.clean(200)
            fake.on_sleep = hook
    fake.on_sleep = hook
    with search.slot("search") as slot:
        order.append("search")
        slot.clean(200)
    assert order == ["description", "search"]
    assert fake.sleeps[:2] == [10, pace_module.YIELD_SECONDS]
    assert _events(description, "kind") == [("description",), ("search",)]


def test_a_caller_that_cannot_get_a_turn_gives_up_without_a_request(make_budget):
    fake = FakeTime()
    description, search = make_budget(fake, give_up_after=10), make_budget(fake)
    description.snapshot()
    _set(description, description_run=2, last_kind="search")  # the share is the search's turn
    search._declare_wanting("search")
    fake.on_sleep = lambda _seconds: search._declare_wanting("search")  # a long walk keeps wanting
    with pytest.raises(PaceBusy):
        with description.slot("description"):
            raise AssertionError("no request may be made")
    assert _row(description, "requests_today") == (0,) and _events(description, "kind") == []
    wanting = _row(description, "wanting")[0]
    assert "description" not in wanting and "search" in wanting
    assert set(fake.sleeps) == {pace_module.YIELD_SECONDS} and sum(fake.sleeps) == pytest.approx(10)


def test_giving_up_writes_only_the_markers_so_another_services_refusal_survives(make_budget):
    """The processor unlocks, the collector is refused in that instant, then the processor gives up.
    The processor loaded the row before the collector wrote it, so any save of its state on the way
    out would overwrite the refusal. The patched unlock puts the collector's request exactly there."""
    fake = FakeTime()
    collector, processor = make_budget(fake), make_budget(fake, give_up_after=0.1)
    collector.snapshot()
    _set(collector, description_run=2, last_kind="description")  # the share is the search's turn
    collector._declare_wanting("search")       # so the processor yields its turn
    refused = []
    real_unlock = processor._unlock

    def unlock_then_the_collector_is_refused():
        real_unlock()
        if not refused:
            with collector.slot("search") as slot:
                slot.throttled(429)
                refused.append(fake.now)
    processor._unlock = unlock_then_the_collector_is_refused
    with pytest.raises(PaceBusy):
        with processor.slot("description"):
            raise AssertionError("no request may be made")
    at = refused[0]
    # The fixed pace spaces the next slot 75 s out; the refusal wait is separate.
    assert _row(collector, "requests_today", "refusal_streak", "cooldown_until", "next_slot_at", "last_kind") == (
        1, 1, at + timedelta(seconds=60), at + timedelta(seconds=75), "search")
    assert "description" not in _row(collector, "wanting")[0]


def test_a_closed_gate_writes_only_the_markers(make_budget):
    fake = FakeTime(datetime(2026, 10, 13, 23, 0, tzinfo=timezone.utc))  # 01:00 Rome: window closed
    budget = make_budget(fake, WINDOWED)
    budget.snapshot()
    _set(budget, last_kind="search", requests_today=7, day=date(2026, 10, 14))
    with pytest.raises(PaceGateClosed):
        with budget.slot("description", overdue=True):
            raise AssertionError("no request may be made")
    assert _row(budget, "last_kind", "requests_today", "wanting") == ("search", 7, {})


def test_a_long_refusal_wait_is_slept_in_chunks_that_beat_the_heartbeat(make_budget, pulses):
    fake = FakeTime()
    budget = make_budget(fake)
    budget.snapshot()
    _set(budget, cooldown_until=fake.now + timedelta(seconds=1800))
    with budget.slot("search") as slot:
        slot.clean(200)
    assert max(fake.sleeps) <= pace_module.WAIT_CHUNK_SECONDS and sum(fake.sleeps) == pytest.approx(1800)
    assert len([p for p in pulses if "linkedin_wait" in p]) == len(fake.sleeps)


def test_a_stale_search_marker_does_not_hold_back_a_description(make_budget):
    fake = FakeTime()
    budget = make_budget(fake)
    budget.snapshot()
    _set(budget, wanting=Json({"search": fake.now.timestamp() - 600}))
    with budget.slot("description") as slot:
        slot.clean(200)
    assert fake.sleeps == []


def test_a_marker_declared_while_a_slot_is_held_survives_its_release(make_budget):
    fake = FakeTime()
    search, other = make_budget(fake), make_budget(fake)
    with search.slot("search") as slot:
        other._declare_wanting("description")  # the description caller arrives mid-request
        slot.clean(200)
    wanting = search.snapshot().wanting
    assert "description" in wanting and "search" not in wanting


def test_managed_pace_forwards_give_up_after(store_conn, store_conn_factory):
    fake = FakeTime()
    managed = ManagedPace(lambda: store_conn_factory(store_conn), clock=fake.clock, sleep=fake.sleep,
                          uniform=lambda a, b: (a + b) / 2, give_up_after=10)
    with managed.slot("description", overdue=True) as slot:
        slot.clean(200)
    assert managed._current().give_up_after == 10
    managed._current().conn.close()


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


def test_an_overdue_next_in_line_description_gives_up_on_a_long_wait(make_budget):
    fake = FakeTime()
    budget = make_budget(fake, give_up_after=120)
    budget.snapshot()
    _set(budget, last_kind="search", next_slot_at=fake.now,
         cooldown_until=fake.now + timedelta(seconds=1800))
    with pytest.raises(PaceBusy):
        with budget.slot("description", overdue=True):
            raise AssertionError("no request may be made")
    assert _row(budget, "requests_today") == (0,) and _events(budget, "kind") == []
    wanting = _row(budget, "wanting")[0]
    assert "description" not in wanting


SAT_NOON = datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc)  # 12:00 Rome, Saturday


def _weekend_request_right_after_a_request(make_budget):
    fake = FakeTime(SAT_NOON)
    first = make_budget(fake)
    with first.slot("search") as slot:
        slot.clean(200)
    budget = make_budget(fake, give_up_after=120)
    # the previous request drew the longest jittered spacing of the weekend window
    _set(budget, next_slot_at=fake.now + timedelta(seconds=132))
    return fake, budget


def test_a_packaging_request_outwaits_the_longest_weekend_spacing(make_budget):
    fake, budget = _weekend_request_right_after_a_request(make_budget)
    started = fake.now
    with budget.slot("packaging") as slot:
        slot.clean(200)
    assert (fake.now - started).total_seconds() == pytest.approx(132)
    assert [kind for (kind,) in _events(budget, "kind")] == ["search", "packaging"]


def test_a_description_request_still_gives_up_at_the_tick_budget_on_a_weekend(make_budget):
    fake, budget = _weekend_request_right_after_a_request(make_budget)
    with pytest.raises(PaceBusy):
        with budget.slot("description"):
            raise AssertionError("no request may be made")
    assert [kind for (kind,) in _events(budget, "kind")] == ["search"]
