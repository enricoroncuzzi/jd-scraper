"""A service stop must end a paced walk promptly, without losing or doubling progress.

Production evidence (2026-10-22): every restart of `jds-collect-linkedin` ran into
systemd's `TimeoutStopSec=300` and was SIGKILLed. A tick is one whole walk, a walk is
up to 90 paced LinkedIn requests at 75 s spacing, and the pace wait was a plain sleep
that never looked at the stop event, so the SIGTERM was only honoured minutes later
(at best). These tests drive the real walk and the real pace with a fake clock and a
fake HTTP fetch, set the stop event mid-walk, and pin what happens then:
no new request, the pace ladder records nothing, and the finished part of the walk is
committed so the next start resumes exactly there.
"""
import threading
from datetime import datetime, timedelta, timezone

from src.linkedin.catalog import CollectorConfig, SliceSpec, sync_slices
from src.linkedin.collector import collect_once
from src.linkedin.pace import ManagedPace, PaceBudget
from src.linkedin.schedule import ScheduleConfig
from tests.linkedin_fakes import ScriptedGet, ids, page_html, response

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
H = 3600
CFG = CollectorConfig(due_hours={1: 12}, max_pages_per_slice=90, known_pages_to_stop=3,
                      legacy_lock_path=None)


class FakeTime:
    """Simulated clock: sleep advances it, and `on_sleep` can react inside a wait.
    `before_sleep` is the time the sleep started, so a hook can measure a wait that
    was cut short (`now` has already advanced by the time the hook runs)."""

    def __init__(self, now=NOW):
        self.now = now
        self.before_sleep = now
        self.sleeps = []
        self.on_sleep = None

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.before_sleep = self.now
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)
        if self.on_sleep:
            self.on_sleep(seconds)


class EndlessPages:
    """A LinkedIn search that always answers a full page of fresh cards, so a walk
    keeps going until its page cap unless something stops it."""

    def __init__(self):
        self.calls = []
        self.on_call = None

    def __call__(self, url, params=None, headers=None, timeout=None):
        self.calls.append(dict(params or {}))
        if self.on_call:
            self.on_call(len(self.calls))
        return response(200, page_html(*ids(int(params["start"]), 10)))


def _budget(store_conn, store_conn_factory, fake, *, stop):
    conn = store_conn_factory(store_conn)
    conn.autocommit = True
    return PaceBudget(conn, clock=fake.clock, sleep=fake.sleep, uniform=lambda a, b: (a + b) / 2,
                      schedule=ScheduleConfig(), service="collect-linkedin", stop=stop)


def _seed(conn):
    sync_slices(conn, [SliceSpec("linkedin", "AI Engineer", "Italy", 1, 12 * H)], NOW)
    conn.commit()


def _rows(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def test_a_stop_during_the_pace_wait_makes_no_further_request_and_resumes_there(
        store_conn, store_conn_factory):
    fake, stop, get = FakeTime(), threading.Event(), EndlessPages()
    budget = _budget(store_conn, store_conn_factory, fake, stop=stop)
    stopped_at = []

    def stop_as_soon_as_the_walk_waits(seconds):
        if get.calls and not stop.is_set():
            stop.set()
            stopped_at.append(fake.before_sleep)  # the moment the wait began

    fake.on_sleep = stop_as_soon_as_the_walk_waits
    _seed(store_conn)

    tick = collect_once(store_conn, budget, CFG, now=NOW, get=get, sleep=fake.sleep, stop=stop)

    assert len(get.calls) == 1  # the page in flight finished, no second request
    assert tick.detail["stop_reason"] == "stopped"
    assert stopped_at, "the walk never reached a pace wait, so nothing was interrupted"
    assert fake.now - stopped_at[0] < timedelta(seconds=2)  # the 75 s wait was cut, not slept

    # The page that did go out is committed, and the slice resumes exactly after it.
    walks = _rows(store_conn, "SELECT pages, cards_seen, new_jobs, stop_reason FROM slice_walks")
    assert walks == [(1, 10, 10, "stopped")]
    (resume_start, last_walked_at, due_at, interval) = _rows(
        store_conn, "SELECT resume_start, last_walked_at, due_at, interval_seconds FROM slices")[0]
    assert resume_start == 10
    assert last_walked_at is None  # a stopped walk is unfinished: the window keeps reaching back
    assert due_at <= NOW + timedelta(minutes=1)  # due at once so the next start resumes it
    assert interval == 12 * H  # a stop never touches the cadence

    # A stop is not a refusal and not a block: only the request that really went out is on
    # record, and the pace leaves no wanting marker behind.
    assert _rows(store_conn, "SELECT outcome, status FROM pace_events") == [("ok", 200)]
    assert _rows(store_conn, "SELECT wanting FROM pace_budgets") == [({},)]


def test_a_stop_between_pages_makes_no_request_even_when_the_turn_is_free(
        store_conn, store_conn_factory):
    fake, stop, get = FakeTime(), threading.Event(), EndlessPages()
    budget = _budget(store_conn, store_conn_factory, fake, stop=stop)
    get.on_call = lambda n: stop.set()  # the stop lands while the first request is in flight
    _seed(store_conn)

    tick = collect_once(store_conn, budget, CFG, now=NOW, get=get, sleep=fake.sleep, stop=stop)

    assert len(get.calls) == 1
    assert tick.detail["stop_reason"] == "stopped"
    assert fake.sleeps == []  # it never even waited for the next turn
    assert _rows(store_conn, "SELECT resume_start FROM slices") == [(10,)]


def test_a_stop_cuts_a_refusal_wait_short_and_never_records_a_refusal(
        store_conn, store_conn_factory):
    fake, stop, get = FakeTime(), threading.Event(), EndlessPages()
    budget = _budget(store_conn, store_conn_factory, fake, stop=stop)
    with budget.conn.cursor() as cur:  # a refusal just happened: the host waits 30 minutes
        cur.execute("INSERT INTO pace_budgets (host, next_slot_at, cooldown_until, refusal_streak) "
                    "VALUES ('linkedin.com', %s, %s, 2)", (NOW, NOW + timedelta(minutes=30)))
    fake.on_sleep = lambda _seconds: stop.set()
    _seed(store_conn)
    before = _rows(store_conn, "SELECT next_slot_at, cooldown_until, refusal_streak, "
                              "requests_today FROM pace_budgets")

    tick = collect_once(store_conn, budget, CFG, now=NOW, get=get, sleep=fake.sleep, stop=stop)

    assert get.calls == [] and tick.detail["stop_reason"] == "stopped"
    assert fake.now - NOW < timedelta(seconds=5)  # the 30 minute wait was cut, not slept through
    assert before == _rows(store_conn, "SELECT next_slot_at, cooldown_until, refusal_streak, "
                                       "requests_today FROM pace_budgets")
    assert _rows(store_conn, "SELECT count(*) FROM pace_events") == [(0,)]
    assert _rows(store_conn, "SELECT wanting FROM pace_budgets") == [({},)]
    assert _rows(store_conn, "SELECT pages, stop_reason FROM slice_walks") == [(0, "stopped")]


def test_a_stop_while_a_request_is_in_flight_lets_it_finish_and_records_it_cleanly(
        store_conn, store_conn_factory):
    fake, stop, get = FakeTime(), threading.Event(), EndlessPages()
    budget = _budget(store_conn, store_conn_factory, fake, stop=stop)
    get.on_call = lambda n: stop.set()  # the stop lands while the request is in flight
    _seed(store_conn)

    tick = collect_once(store_conn, budget, CFG, now=NOW, get=get, sleep=fake.sleep, stop=stop)

    assert len(get.calls) == 1
    assert tick.detail["stop_reason"] == "stopped"
    assert _rows(store_conn, "SELECT outcome, status, requests_today FROM pace_events") == [("ok", 200, 1)]
    assert _rows(store_conn, "SELECT count(*) FROM sightings") == [(10,)]
    assert _rows(store_conn, "SELECT resume_start FROM slices") == [(10,)]


def test_a_stop_before_the_walk_claims_a_slice_does_not_claim_one(
        store_conn, store_conn_factory):
    fake, stop, get = FakeTime(), threading.Event(), EndlessPages()
    budget = _budget(store_conn, store_conn_factory, fake, stop=stop)
    _seed(store_conn)
    stop.set()

    tick = collect_once(store_conn, budget, CFG, now=NOW, get=get, sleep=fake.sleep, stop=stop)

    assert not tick.did_work and get.calls == []
    assert _rows(store_conn, "SELECT count(*) FROM slice_walks") == [(0,)]
    assert _rows(store_conn, "SELECT due_at FROM slices") == [(NOW,)]


def test_the_managed_pace_carries_the_stop_event_into_the_budget(store_conn,
                                                                store_conn_factory):
    """The wrapper the collector really builds in `main`: the stop event has to ride
    through `ManagedPace`'s extra budget options. Without it the pace would sleep out the
    whole 30 minute refusal wait and the stop would only land minutes later."""
    fake, stop, get = FakeTime(), threading.Event(), EndlessPages()
    pace = ManagedPace(lambda: store_conn_factory(store_conn), service="collect-linkedin",
                       schedule=ScheduleConfig(), clock=fake.clock, sleep=fake.sleep,
                       uniform=lambda a, b: (a + b) / 2, stop=stop)
    with store_conn.cursor() as cur:  # a refusal just happened: the host waits 30 minutes
        cur.execute("INSERT INTO pace_budgets (host, next_slot_at, cooldown_until, refusal_streak) "
                    "VALUES ('linkedin.com', %s, %s, 2)", (NOW, NOW + timedelta(minutes=30)))
    store_conn.commit()
    fake.on_sleep = lambda _seconds: stop.set()
    _seed(store_conn)

    tick = collect_once(store_conn, pace, CFG, now=NOW, get=get, sleep=fake.sleep, stop=stop)

    assert get.calls == [] and tick.detail["stop_reason"] == "stopped"
    assert fake.now - NOW < timedelta(seconds=5)  # the refusal wait was cut, not slept through
    assert _rows(store_conn, "SELECT count(*) FROM pace_events") == [(0,)]


def test_main_hands_one_stop_event_to_the_pace_the_tick_and_the_runner(monkeypatch):
    """What makes the pace wait interruptible in production: the collector's own stop
    event is the one SIGTERM sets, so the pace sees it inside the wait."""
    from src.linkedin import collector as collector_module

    seen = {}

    class FakePace:
        def __init__(self, connect_fn, *, service, schedule, stop=None):
            seen["pace"] = stop

    def fake_run_service(name, tick, *, connect_fn, stop=None):
        seen["runner"] = stop
        return 0

    monkeypatch.setattr(collector_module, "ManagedPace", FakePace)
    monkeypatch.setattr(collector_module, "run_service", fake_run_service)
    monkeypatch.setattr(collector_module, "load_dotenv", lambda *a, **k: None)

    assert collector_module.main([]) == 0
    assert isinstance(seen["pace"], threading.Event)
    assert seen["pace"] is seen["runner"]


def test_the_next_start_resumes_a_stopped_walk_without_losing_or_doubling_a_card(
        store_conn, store_conn_factory):
    """The stop itself is only half of it: the next start must pick the slice up where
    the committed page ended, recording every remaining card exactly once."""
    fake, stop, get = FakeTime(), threading.Event(), EndlessPages()
    budget = _budget(store_conn, store_conn_factory, fake, stop=stop)
    get.on_call = lambda n: stop.set() if n == 1 else None
    _seed(store_conn)

    first = collect_once(store_conn, budget, CFG, now=NOW, get=get, sleep=fake.sleep, stop=stop)

    assert first.detail["stop_reason"] == "stopped"
    assert _rows(store_conn, "SELECT count(*) FROM sightings") == [(10,)]

    later, get2 = FakeTime(NOW + timedelta(minutes=5)), ScriptedGet(
        response(200, page_html(*ids(10, 10))), response(200, page_html(*ids(20, 10))),
        response(200, page_html()))
    budget2 = _budget(store_conn, store_conn_factory, later, stop=threading.Event())

    second = collect_once(store_conn, budget2, CFG, now=later.now, get=get2, sleep=later.sleep)

    assert [call["start"] for call in get2.calls] == [10, 20, 30]  # no gap, no re-read
    assert second.detail["stop_reason"] == "end_of_results"
    # Page 1 was recorded once by the stopped walk and not read again: 30 distinct cards,
    # seen exactly once each.
    assert _rows(store_conn, "SELECT count(*), sum(seen_count) FROM sightings") == [(30, 30)]
    assert _rows(store_conn, "SELECT count(*) FROM jobs") == [(30,)]
    assert _rows(store_conn, "SELECT resume_start FROM slices") == [(0,)]  # finished walk
