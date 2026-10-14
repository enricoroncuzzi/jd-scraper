import threading

from src.jobstore import heartbeat
from src.services.pulse import MIN_INTERVAL_SECONDS, Pulse, pulse
from src.services.runner import Tick, run_service


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _beat(conn, service="svc"):
    conn.commit()
    return {b.service: b for b in heartbeat.read_all(conn)}.get(service)


def test_a_pulse_beats_running_with_its_detail_over_its_own_connection(store_conn, store_conn_factory):
    beat = Pulse("svc", lambda: store_conn_factory(store_conn), clock=Clock())
    beat({"slice": "AI / Italy"})
    row = _beat(store_conn)
    assert (row.state, row.detail) == ("running", {"slice": "AI / Italy"})
    beat.close()


def test_pulses_are_rate_limited_so_a_fast_loop_does_not_hammer_the_store(store_conn, store_conn_factory):
    clock, opened = Clock(), []

    def connect():
        opened.append(1)
        return store_conn_factory(store_conn)

    beat = Pulse("svc", connect, clock=clock)
    beat({"n": 1})
    clock.now += MIN_INTERVAL_SECONDS - 1
    beat({"n": 2})
    assert _beat(store_conn).detail == {"n": 1}
    clock.now += 2
    beat({"n": 3})
    assert _beat(store_conn).detail == {"n": 3} and len(opened) == 1
    beat.close()


def test_a_failing_heartbeat_never_raises_and_reconnects_next_time(store_conn, store_conn_factory, capsys):
    clock, attempts = Clock(), []

    def connect():
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("store down")
        return store_conn_factory(store_conn)

    beat = Pulse("svc", connect, clock=clock)
    beat({"n": 1})
    assert "heartbeat failed" in capsys.readouterr().out
    clock.now += MIN_INTERVAL_SECONDS + 1
    beat({"n": 2})
    assert _beat(store_conn).detail == {"n": 2}
    beat.close()


def test_pulse_outside_a_service_does_nothing():
    pulse({"n": 1})


def test_a_service_shows_running_while_one_tick_is_still_working(store_conn, store_conn_factory):
    """A long tick (a slow LinkedIn walk) used to leave the row 'starting' until it ended."""
    seen = {}

    def tick(conn):
        pulse({"slice": "AI / Italy", "pages": 2})
        seen["mid_tick"] = _beat(store_conn)
        stop.set()
        return Tick(True, {"done": True})

    stop = threading.Event()
    run_service("svc", tick, connect_fn=lambda: store_conn_factory(store_conn), idle_seconds=7,
                stop=stop, wait=lambda _s: None, install_signals=False)
    assert (seen["mid_tick"].state, seen["mid_tick"].detail) == ("running", {"slice": "AI / Italy", "pages": 2})


def test_the_pulse_is_removed_when_the_service_ends(store_conn, store_conn_factory):
    from src.services import pulse as pulse_module

    stop = threading.Event()
    stop.set()
    run_service("svc", lambda conn: Tick(False), connect_fn=lambda: store_conn_factory(store_conn),
                stop=stop, wait=lambda _s: None, install_signals=False)
    assert pulse_module._active is None
