"""The loop every long-running service shares: heartbeat, graceful stop, crash isolation.

A service is `tick(conn) -> Tick`, called again and again. SIGTERM (what
`systemctl stop` sends) sets one `threading.Event`, and the loop honours it between
ticks, so a deploy never interrupts a tick halfway. A tick that can run longer than a
few seconds - the LinkedIn collector's tick is one whole paced walk, up to 90 requests
at 75 s spacing - must take that same event (`stop=`) and consult it, and sleep through
`sleep_unless_stopped`, or the stop would only be honoured when the tick is done and
systemd would kill the process at `TimeoutStopSec`. An exception in a tick is printed,
rolled back, and retried with backoff; it never ends the service. systemd's Restart=
only has to cover the process itself dying.
"""
import signal
import threading
import zlib
from dataclasses import dataclass, field

from src.jobstore import heartbeat
from src.jobstore.db import connect
from src.jobstore.schema import init_store
from src.services import pulse as pulse_module

MAX_BACKOFF_SECONDS = 300
# How long a stop request may sit unnoticed inside one sleep. Small enough that a
# paced wait ends in seconds (the systemd stop timeout is 300 s), large enough that a
# long wait is not thousands of wake-ups.
STOP_GRANULARITY_SECONDS = 1.0


def sleep_unless_stopped(sleep, stop: threading.Event | None, seconds: float, *,
                        granularity: float = STOP_GRANULARITY_SECONDS) -> bool:
    """Sleep `seconds` through `sleep`, in slices of at most `granularity`, and return
    True when a stop has been requested (then the caller must not start more work).

    A request already in flight is never interrupted: only a wait is. Without a stop
    event this is one plain sleep, exactly as an unshared service always did.
    """
    if stop is None:
        sleep(seconds)
        return False
    remaining = seconds
    while remaining > 0:
        if stop.is_set():
            return True
        slice_seconds = min(remaining, granularity)
        sleep(slice_seconds)
        remaining -= slice_seconds
    return stop.is_set()


@dataclass
class Tick:
    did_work: bool
    detail: dict = field(default_factory=dict)


def acquire_single_instance(conn, name: str) -> bool:
    """Hold a session advisory lock named after the service, so a second copy
    started by mistake exits instead of working in parallel."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (zlib.crc32(f"jds-service:{name}".encode()),))
        return cur.fetchone()[0]


def _install_signal_handlers(stop: threading.Event) -> None:
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda _signum, _frame: stop.set())


def run_service(name: str, tick, *, connect_fn=connect, idle_seconds: float = 60.0,
                stop: threading.Event | None = None, wait=None,
                install_signals: bool = True) -> int:
    """Run `tick` until stopped. Returns a process exit code (0 clean, 1 duplicate)."""
    stop = stop or threading.Event()
    wait = wait or stop.wait
    if install_signals:
        _install_signal_handlers(stop)
    beat_during_tick = pulse_module.Pulse(name, connect_fn)
    pulse_module.install(beat_during_tick)
    print(f"[{name}] starting")
    try:
        return _loop(name, tick, connect_fn, idle_seconds, stop, wait)
    finally:
        pulse_module.install(None)
        beat_during_tick.close()


def _loop(name, tick, connect_fn, idle_seconds, stop, wait) -> int:
    conn = None
    failures = 0
    while not stop.is_set():
        try:
            if conn is None or conn.closed:
                conn = connect_fn()
                init_store(conn)
                if not acquire_single_instance(conn, name):
                    print(f"[{name}] another instance is already running; exiting")
                    conn.close()
                    return 1
                heartbeat.start(conn, name)
                conn.commit()
            result = tick(conn)
            conn.commit()
            heartbeat.beat(conn, name, "running" if result.did_work else "idle", result.detail)
            conn.commit()
            failures = 0
            if not result.did_work:
                wait(idle_seconds)
        except Exception as exc:  # noqa: BLE001 - a tick must never end the service
            failures += 1
            print(f"[{name}] tick failed ({type(exc).__name__}: {exc}); retry {failures}")
            try:
                conn.rollback()
                heartbeat.beat(conn, name, "error", {"error": f"{type(exc).__name__}: {exc}"[:200]})
                conn.commit()
            except Exception:  # noqa: BLE001 - the connection itself may be gone
                conn = None
            wait(min(5 * 2 ** (failures - 1), MAX_BACKOFF_SECONDS))
    if conn is not None and not conn.closed:
        try:
            heartbeat.beat(conn, name, "stopped")
            conn.commit()
        finally:
            conn.close()
    print(f"[{name}] stopped")
    return 0
