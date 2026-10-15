"""Heartbeats from inside a long tick.

`run_service` beats once per finished tick. A LinkedIn walk or a batch of paced
description fetches can run for hours once the pace backs off, so the loops that
make progress call `pulse()` and the service stays visibly alive while it works.
Progress points beat, not a timer thread: a tick that is wedged still goes silent
and the watchdog still sees it.

The beat goes over its own autocommit connection, so it never commits or
disturbs the tick's open transaction. A failed beat is logged and dropped.
"""
import time

from src.jobstore import heartbeat

MIN_INTERVAL_SECONDS = 30.0


class Pulse:
    def __init__(self, service: str, connect_fn, *, clock=time.monotonic):
        self._service = service
        self._connect = connect_fn
        self._clock = clock
        self._conn = None
        self._last: float | None = None

    def __call__(self, detail: dict | None = None) -> None:
        now = self._clock()
        if self._last is not None and now - self._last < MIN_INTERVAL_SECONDS:
            return
        self._last = now
        try:
            if self._conn is None or self._conn.closed:
                self._conn = self._connect()
                self._conn.autocommit = True
            heartbeat.beat(self._conn, self._service, "running", detail)
        except Exception as exc:  # noqa: BLE001 - a missed beat must never stop the work
            print(f"[{self._service}] heartbeat failed ({type(exc).__name__}: {exc})")
            self.close()

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - already gone
                pass


_active: Pulse | None = None


def install(pulse_: Pulse | None) -> None:
    global _active
    _active = pulse_


def pulse(detail: dict | None = None) -> None:
    """Beat for the running service; does nothing outside `run_service`."""
    if _active is not None:
        _active(detail)
