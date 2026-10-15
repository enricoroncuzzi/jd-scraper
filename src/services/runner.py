"""The loop every long-running service shares: heartbeat, graceful stop, crash isolation.

A service is `tick(conn) -> Tick`, called again and again. SIGTERM (what
`systemctl stop` sends) is honoured between ticks, so a deploy never interrupts
a tick halfway. An exception in a tick is printed, rolled back, and retried
with backoff; it never ends the service. systemd's Restart= only has to cover
the process itself dying.
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
