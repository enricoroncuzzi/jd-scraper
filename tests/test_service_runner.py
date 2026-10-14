import threading

import pytest

from src.jobstore import heartbeat
from src.services.legacy_guard import legacy_run_active
from src.services.runner import Tick, acquire_single_instance, run_service


@pytest.fixture
def run(store_conn_factory, store_conn):
    def go(tick, *, stop=None, **kwargs):
        stop = stop or threading.Event()
        waits = []
        code = run_service(
            "svc", tick, connect_fn=lambda: store_conn_factory(store_conn), idle_seconds=7,
            stop=stop, wait=waits.append, install_signals=False, **kwargs)
        return code, waits
    return go


def _beats(conn):
    conn.commit()
    return {beat.service: beat for beat in heartbeat.read_all(conn)}


def test_ticks_run_until_stop_and_the_service_ends_stopped(run, store_conn):
    stop, calls = threading.Event(), []

    def tick(conn):
        calls.append(1)
        if len(calls) == 3:
            stop.set()
        return Tick(True, {"n": len(calls)})

    code, waits = run(tick, stop=stop)
    assert code == 0 and len(calls) == 3 and waits == []
    assert _beats(store_conn)["svc"].state == "stopped"


def test_an_idle_tick_waits_before_the_next(run):
    stop, calls = threading.Event(), []

    def tick(conn):
        calls.append(1)
        if len(calls) == 2:
            stop.set()
        return Tick(False)

    _, waits = run(tick, stop=stop)
    assert waits == [7, 7]


def test_a_failing_tick_is_retried_with_backoff_and_never_ends_the_service(run, store_conn):
    stop, calls = threading.Event(), []

    def tick(conn):
        calls.append(1)
        if len(calls) <= 2:
            raise RuntimeError("boom")
        stop.set()
        return Tick(True)

    code, waits = run(tick, stop=stop)
    assert code == 0 and len(calls) == 3
    assert waits == [5, 10]


def test_a_tick_that_fails_after_writing_is_rolled_back(run, store_conn):
    stop, calls = threading.Event(), []

    def tick(conn):
        calls.append(1)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO kv_state (key) VALUES ('half-done')")
        if len(calls) == 1:
            raise RuntimeError("after the write")
        stop.set()
        return Tick(True)

    run(tick, stop=stop)
    store_conn.commit()
    with store_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM kv_state WHERE key = 'half-done'")
        assert cur.fetchone()[0] == 1  # the failed attempt's insert did not survive


def test_a_second_instance_exits_with_code_1(run, store_conn_factory, store_conn):
    holder = store_conn_factory(store_conn)
    assert acquire_single_instance(holder, "svc")
    code, _ = run(lambda conn: Tick(True))
    assert code == 1
    holder.close()


def test_the_legacy_guard_sees_a_held_flock(tmp_path):
    import fcntl
    lock = tmp_path / "jd-scraper.lock"
    assert legacy_run_active(str(lock)) is False  # missing file
    lock.write_text("")
    assert legacy_run_active(str(lock)) is False  # present, nobody holds it
    assert legacy_run_active(None) is False
    with open(lock, "a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        assert legacy_run_active(str(lock)) is True
    assert legacy_run_active(str(lock)) is False
