"""Shared LinkedIn pace budget (spec sections 4.1-4.4).

One budget per host, stored in the `pace_budgets` table and guarded by a
Postgres session advisory lock, so the collector and the processor cannot
exceed it together and never have two LinkedIn requests in flight.

Two things gate every request, all enforced here so no caller can bypass them:
the quiet window (config/schedule.json `closed_hours`, Rome time) and the shared
daily request budget (weekday/weekend cap, Rome calendar day). When the gate is
closed `slot` raises PaceGateClosed instead of blocking a tick for hours; the
services catch that and go idle. Every request is recorded in `pace_events`.

The pace is fixed (2026-10-18 refusal study): a constant base spacing per window
(75 s weekday / 110 s weekend, never below the cap-derived floor) jittered +/-20 %.
Nothing slows it down: the study found the refusal rate did not depend on the gap
anywhere between 3 s and 390 s, while the old hour nudge and day verdict cut
throughput to about a quarter. A refusal is waited out (60 s doubling to a 30 min
cap, or `Retry-After`) and the pace carries on unchanged. When both a search and a
description are waiting, descriptions take two of every three turns
(`pace.description_turn_share`); a lone waiting kind always takes the turn. The
advisory lock is held only for the request itself; waiters sleep unlocked in short
chunks. A caller that passes `give_up_after` and cannot get a turn raises PaceBusy,
and a caller that gives up writes only its own `wanting` markers.
"""
import json
import random
import time
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from src.jobstore.db import connect, store_url
from src.linkedin.schedule import (
    RefusalPolicy,
    ScheduleConfig,
    daily_cap,
    floor_spacing_seconds,
    in_closed_window,
    local_day,
    start_spacing_seconds,
    window_name,
)
from src.services.pulse import pulse

HOST = "linkedin.com"
WANTING_FRESH_SECONDS = 30.0
LOCK_POLL_SECONDS = 0.25
YIELD_SECONDS = 0.5
WAIT_CHUNK_SECONDS = 15.0

EVENT_OUTCOMES = {
    "clean": "ok",
    "throttled": "refused",
    "neutral": "neutral",
}


@dataclass(frozen=True)
class Gate:
    """Why LinkedIn is open or closed to both services right now."""

    open: bool
    reason: str  # open | closed_hours | daily_cap
    requests_today: int
    cap: int
    window: str  # weekday | weekend


class PaceGateClosed(RuntimeError):
    """The schedule gate says no LinkedIn request may be made now. `gate` says why."""

    def __init__(self, gate: Gate):
        super().__init__(f"LinkedIn gate closed ({gate.reason})")
        self.gate = gate


class PaceBusy(RuntimeError):
    """No LinkedIn turn was granted within `give_up_after`; nothing was sent."""


def evaluate_gate(schedule: ScheduleConfig, now: datetime, *, requests_today: int) -> Gate:
    cap = daily_cap(schedule, now)
    window = window_name(schedule, now)
    common = {"requests_today": requests_today, "cap": cap, "window": window}
    if in_closed_window(schedule, now):
        return Gate(False, "closed_hours", **common)
    if requests_today >= cap:
        return Gate(False, "daily_cap", **common)
    return Gate(True, "open", **common)


@dataclass
class PaceState:
    next_slot_at: datetime
    last_kind: str | None = None
    wanting: dict = field(default_factory=dict)
    requests_today: int = 0
    refusals_today: int = 0
    day: date | None = None
    wait_until: datetime | None = None
    refusal_streak: int = 0
    last_clean_at: datetime | None = None
    description_run: int = 0


def description_run_cap(share: float) -> int:
    """The most consecutive description turns the share allows when both kinds wait.
    A share p means p:(1-p) descriptions to searches, so the run is p/(1-p),
    rounded to the nearest whole turn and never below one."""
    return max(1, round(share / (1.0 - share)))


def next_spacing(schedule: ScheduleConfig, now: datetime,
                 uniform=random.uniform) -> float:
    """The next sleep: the fixed base spacing for `now`'s window, jittered by
    +/-`pace.jitter`, never below the cap-derived floor."""
    window = window_name(schedule, now)
    base = start_spacing_seconds(schedule, window)
    factor = uniform(1 - schedule.pace.jitter, 1 + schedule.pace.jitter)
    return max(floor_spacing_seconds(schedule, window), base * factor)


def refusal_wait_seconds(streak: int, retry_after: float | None, policy: RefusalPolicy,
                         uniform=random.uniform, *, jitter: float) -> float:
    """How long to wait after the `streak`-th consecutive refusal. `Retry-After`
    wins when present; otherwise the first wait doubles per refusal. Both are capped
    at `max_wait_seconds` (the anti-shutdown guarantee)."""
    if retry_after is not None:
        return min(retry_after, policy.max_wait_seconds)
    base = policy.first_wait_seconds * 2 ** max(streak - 1, 0)
    return min(base * uniform(1 - jitter, 1 + jitter), policy.max_wait_seconds)


def roll_day(state: PaceState, schedule: ScheduleConfig, now: datetime) -> PaceState:
    """When the Rome calendar day has moved on, set the new day and zero the counters.
    The pace itself is fixed, so a finished day changes nothing else."""
    day = local_day(schedule, now)
    if state.day != day:
        state.day = day
        state.requests_today = 0
        state.refusals_today = 0
    return state


def _fresh(state: PaceState, marker: str, now_ts: float) -> bool:
    stamp = state.wanting.get(marker)
    return stamp is not None and now_ts - stamp <= WANTING_FRESH_SECONDS


def must_yield(state: PaceState, kind: str, now_ts: float, *, run_cap: int) -> bool:
    """When both a search and a description are waiting, descriptions take two of every
    three turns: a description yields only once `run_cap` descriptions have gone in a
    row, and a search yields while fewer than `run_cap` have. A lone waiting kind always
    takes the turn."""
    search_waiting = _fresh(state, "search", now_ts)
    description_waiting = _fresh(state, "description", now_ts)
    if kind == "description":
        return search_waiting and state.description_run >= run_cap
    if kind == "search":
        return description_waiting and state.description_run < run_cap
    return False


def _lock_key(host: str) -> int:
    return zlib.crc32(f"jds-pace:{host}".encode())


class Slot:
    """The caller's handle on one granted request turn."""

    def __init__(self):
        self.outcome = "neutral"
        self.status: int | None = None
        self.retry_after: float | None = None
        self.wait_seconds: float | None = None

    def clean(self, status: int | None = None) -> None:
        self.outcome = "clean"
        self.status = status

    def throttled(self, status: int | None = None, retry_after: float | None = None) -> None:
        self.outcome = "throttled"
        self.status = status
        self.retry_after = retry_after

    def neutral(self, status: int | None = None) -> None:
        self.outcome = "neutral"
        self.status = status


class PaceBudget:
    """Use `with pace.slot("search") as slot:` around exactly one LinkedIn request
    and call slot.clean() or slot.throttled() with what came back. When the
    schedule gate is closed the `with` raises PaceGateClosed and no request is
    made, so a caller must never wrap the request in a bare try/except that would
    swallow it.

    `conn` must be a dedicated autocommit connection: the advisory lock lives as
    long as the session, so a crashed service releases it by disconnecting.
    """

    def __init__(self, conn, host: str = HOST, *, clock=None, sleep=time.sleep,
                 uniform=random.uniform, service: str | None = None,
                 schedule: ScheduleConfig | None = None,
                 give_up_after: float | None = None):
        self.conn = conn
        self.host = host
        self.key = _lock_key(host)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep
        self.uniform = uniform
        self.service = service
        self.schedule = schedule or ScheduleConfig()
        self.give_up_after = give_up_after
        self.run_cap = description_run_cap(self.schedule.pace.description_turn_share)

    # -- storage ---------------------------------------------------------
    def _load(self) -> PaceState:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pace_budgets (host, next_slot_at) VALUES (%s, %s) "
                "ON CONFLICT (host) DO NOTHING", (self.host, self.clock()))
            cur.execute(
                "SELECT next_slot_at, cooldown_until, last_kind, wanting, requests_today, "
                "refusals_today, day, refusal_streak, last_clean_at, description_run "
                "FROM pace_budgets WHERE host = %s", (self.host,))
            (next_slot, wait_until, last_kind, wanting, requests_today, refusals_today, day,
             refusal_streak, last_clean_at, description_run) = cur.fetchone()
        state = PaceState(next_slot, last_kind, dict(wanting or {}), requests_today, refusals_today,
                          day, wait_until, refusal_streak, last_clean_at, description_run or 0)
        return roll_day(state, self.schedule, self.clock())

    def _save(self, state: PaceState, kinds: list[str]) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE pace_budgets SET next_slot_at = %s, cooldown_until = %s, last_kind = %s, "
                "wanting = wanting - %s::text[], requests_today = %s, refusals_today = %s, day = %s, "
                "refusal_streak = %s, last_clean_at = %s, description_run = %s WHERE host = %s",
                (state.next_slot_at, state.wait_until, state.last_kind, kinds,
                 state.requests_today, state.refusals_today, state.day, state.refusal_streak,
                 state.last_clean_at, state.description_run, self.host))

    def _declare_wanting(self, kind: str) -> None:
        markers = {kind: self.clock().timestamp()}
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pace_budgets (host, next_slot_at, wanting) VALUES (%s, %s, %s::jsonb) "
                "ON CONFLICT (host) DO UPDATE SET wanting = pace_budgets.wanting || %s::jsonb",
                (self.host, self.clock(), json.dumps(markers), json.dumps(markers)))

    def _drop_markers(self, kinds: list[str]) -> None:
        """The only write on the give-up paths, and the only lock-free write besides
        the caller's own marker merge: it touches nothing but `wanting`."""
        with self.conn.cursor() as cur:
            cur.execute("UPDATE pace_budgets SET wanting = wanting - %s::text[] WHERE host = %s",
                        (kinds, self.host))

    def _try_lock(self) -> bool:
        with self.conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (self.key,))
            return cur.fetchone()[0]

    def _unlock(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (self.key,))

    def snapshot(self) -> PaceState:
        """Current stored state, for status displays. Takes no lock."""
        return self._load()

    def gate(self) -> Gate:
        """Why LinkedIn is open or closed right now. Takes no lock and no request."""
        state = self._load()
        return evaluate_gate(self.schedule, self.clock(), requests_today=state.requests_today)

    def seconds_until_slot(self) -> float:
        """How long a request made now would wait for its turn, ignoring the other
        service's place in the queue. Takes no lock."""
        state = self._load()
        ready = max(state.next_slot_at, state.wait_until or state.next_slot_at)
        return max(0.0, (ready - self.clock()).total_seconds())

    # -- the turn --------------------------------------------------------
    def _record(self, state: PaceState, kind: str, slot: Slot, now: datetime) -> float:
        """Count the request against the Rome day and advance the fixed pace. The caller
        saves the row first; the pace_events row is written afterwards, so a logging
        failure can never lose the count or the refusal state."""
        roll_day(state, self.schedule, now)
        state.requests_today += 1
        if slot.outcome == "clean":
            state.refusal_streak = 0
            state.wait_until = None
            state.last_clean_at = now
        elif slot.outcome == "throttled":
            state.refusal_streak += 1
            state.refusals_today += 1
            wait = refusal_wait_seconds(state.refusal_streak, slot.retry_after,
                                        self.schedule.refusals, self.uniform,
                                        jitter=self.schedule.pace.jitter)
            slot.wait_seconds = wait
            state.wait_until = now + timedelta(seconds=wait)
            print(f"[pace] LinkedIn refused a {kind} request ({slot.status}): waiting "
                  f"{wait:.0f}s (refusal {state.refusal_streak} in a row)")
        state.description_run = state.description_run + 1 if kind == "description" else 0
        state.last_kind = kind
        spacing = next_spacing(self.schedule, now, self.uniform)
        state.next_slot_at = now + timedelta(seconds=spacing)
        return spacing

    def _log_event(self, state: PaceState, kind: str, slot: Slot, now: datetime,
                   spacing: float) -> None:
        """Append one request to pace_events. Telemetry must never break a run, so a
        failed insert is logged and swallowed; the state is already saved."""
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO pace_events (at, service, kind, outcome, status, "
                    "requests_today, spacing_seconds, wait_seconds) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (now, self.service, kind, EVENT_OUTCOMES.get(slot.outcome, slot.outcome),
                     slot.status, state.requests_today, spacing, slot.wait_seconds))
        except Exception as exc:  # noqa: BLE001 - telemetry must not raise into the pipeline
            print(f"[pace] could not record the {kind} request in pace_events: "
                  f"{type(exc).__name__}: {exc}")

    def _gate(self, state: PaceState) -> Gate:
        return evaluate_gate(self.schedule, self.clock(), requests_today=state.requests_today)

    @contextmanager
    def slot(self, kind: str, *, overdue: bool = False):
        started = self.clock()
        kinds = [kind]
        while True:
            self._declare_wanting(kind)
            if not self._try_lock():
                self.sleep(LOCK_POLL_SECONDS)
                continue
            now = self.clock()
            state = self._load()
            gate = self._gate(state)
            if not gate.open:
                self._drop_markers(kinds)
                self._unlock()
                raise PaceGateClosed(gate)
            ready = max(state.next_slot_at, state.wait_until or state.next_slot_at)
            wait = (ready - now).total_seconds()
            if wait <= 0 and not must_yield(state, kind, now.timestamp(), run_cap=self.run_cap):
                break  # keep the lock: the request goes now
            self._unlock()
            pause = min(wait, WAIT_CHUNK_SECONDS) if wait > 0 else YIELD_SECONDS
            # An overdue description that the share says takes the next turn may spend
            # more than the tick budget waiting, as long as one spacing fits in it; the
            # guard compares the remaining wait, not the elapsed time.
            next_in_line = overdue and state.description_run < self.run_cap
            if self.give_up_after is not None and (
                    wait > self.give_up_after if next_in_line
                    else (self.clock() - started).total_seconds() + pause > self.give_up_after):
                self._drop_markers(kinds)
                raise PaceBusy(f"no LinkedIn {kind} turn within {self.give_up_after:g}s")
            pulse({"linkedin_wait": round(max(wait, 0))})
            self.sleep(pause)
        slot = Slot()
        try:
            yield slot
        finally:
            try:
                now = self.clock()
                spacing = self._record(state, kind, slot, now)
                self._save(state, kinds)
                self._log_event(state, kind, slot, now, spacing)
            finally:
                self._unlock()


class ManagedPace:
    """A PaceBudget that reopens its dedicated autocommit connection when it drops,
    so one lost connection does not wedge a long-running service."""

    def __init__(self, connect, service: str | None = None,
                 schedule: ScheduleConfig | None = None, **budget_options):
        self._connect = connect
        self._options = dict(budget_options, service=service, schedule=schedule)
        self._budget: PaceBudget | None = None

    def _current(self) -> PaceBudget:
        if self._budget is None or self._budget.conn.closed:
            conn = self._connect()
            conn.autocommit = True
            self._budget = PaceBudget(conn, **self._options)
        return self._budget

    def gate(self) -> Gate:
        return self._current().gate()

    def seconds_until_slot(self) -> float:
        return self._current().seconds_until_slot()

    @contextmanager
    def slot(self, kind: str, *, overdue: bool = False):
        with self._current().slot(kind, overdue=overdue) as slot:
            yield slot


def linkedin_pace(service: str, schedule: ScheduleConfig,
                  give_up_after: float | None) -> ManagedPace:
    """The shared LinkedIn pace for one service, with an optional turn budget."""
    return ManagedPace(lambda: connect(store_url()), service=service, schedule=schedule,
                       give_up_after=give_up_after)
