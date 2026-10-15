"""Shared LinkedIn pace budget (spec sections 4.2 and 4.4).

One budget per host, stored in the `pace_budgets` table and guarded by a
Postgres session advisory lock, so the collector and the processor cannot
exceed it together and never have two LinkedIn requests in flight.

Three things gate every request, all enforced here so no caller can bypass them:

* the quiet window (config/schedule.json `closed_hours`, Rome time);
* the shared daily request budget (weekday/weekend cap, Rome calendar day,
  persisted as `requests_today`/`day`);
* the circuit breaker: `breaker.refusals` throttled logical requests inside
  `breaker.window_minutes` start a rest of `breaker.rest_ladder_hours[0]`
  (2 h), and a refused probe after a rest escalates to the next rung (6 h,
  then 24 h, then 24 h on repeat). A probe that is not refused resets the
  ladder and the refusal window.

When the gate is closed `slot` raises PaceGateClosed instead of blocking a tick
for hours; the services catch that and go idle. Every request is recorded in
`pace_events`.

The rate itself still adapts AIMD-style: a throttle (429, 999, throttled empty
page) doubles the spacing and starts a 60 s cooldown. One logical request that
is refused and retried is one throttle: its repeat refusals keep the cooldown
but do not double again, and only the first refusal of a logical request counts
toward the breaker. The rate recovers two ways: every 50 clean responses it
rises 10 percent, and every 30 minutes without a refusal the spacing halves,
back down to the base rate. Search pages and description fetches take turns so
neither starves the other.
"""
import json
import random
import time
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from src.linkedin.schedule import (
    ScheduleConfig, daily_cap, in_closed_window, local_day, window_name,
)

HOST = "linkedin.com"
BASE_SPACING_RANGE = (2.0, 4.0)
FLOOR_SPACING_SECONDS = 300.0
MAX_FACTOR = FLOOR_SPACING_SECONDS / 3.0
COOLDOWN_SECONDS = 60.0
CALM_HALVING_SECONDS = 1800.0
CLEAN_STREAK_FOR_RAISE = 50
RATE_RAISE = 1.10
WANTING_FRESH_SECONDS = 30.0
LOCK_POLL_SECONDS = 0.25
YIELD_SECONDS = 0.5

EVENT_OUTCOMES = {
    "clean": "ok",
    "throttled": "refused",
    "throttled_again": "refused",
    "neutral": "neutral",
}


@dataclass(frozen=True)
class Gate:
    """Why LinkedIn is open or closed to both services right now."""

    open: bool
    reason: str  # open | closed_hours | daily_cap | breaker
    requests_today: int
    cap: int
    window: str  # weekday | weekend
    rest_until: datetime | None = None
    rest_level: int = 0


class PaceGateClosed(RuntimeError):
    """The schedule gate says no LinkedIn request may be made now. `gate` says why."""

    def __init__(self, gate: Gate):
        super().__init__(f"LinkedIn gate closed ({gate.reason})")
        self.gate = gate


def evaluate_gate(schedule: ScheduleConfig, now: datetime, *, requests_today: int,
                  rest_until: datetime | None = None, rest_level: int = 0) -> Gate:
    cap = daily_cap(schedule, now)
    window = window_name(schedule, now)
    common = {"requests_today": requests_today, "cap": cap, "window": window,
              "rest_until": rest_until, "rest_level": rest_level}
    if rest_until is not None and rest_until > now:
        return Gate(False, "breaker", **common)
    if in_closed_window(schedule, now):
        return Gate(False, "closed_hours", **common)
    if requests_today >= cap:
        return Gate(False, "daily_cap", **common)
    return Gate(True, "open", **common)


@dataclass
class PaceState:
    next_slot_at: datetime
    factor: float = 1.0
    clean_streak: int = 0
    cooldown_until: datetime | None = None
    last_kind: str | None = None
    wanting: dict[str, float] = field(default_factory=dict)
    calm_since: datetime | None = None
    requests_today: int = 0
    day: date | None = None
    refusal_times: list[datetime] = field(default_factory=list)
    rest_until: datetime | None = None
    rest_level: int = 0


def roll_day(state: PaceState, schedule: ScheduleConfig, now: datetime) -> PaceState:
    """Zero the shared counter when the Rome calendar day has moved on."""
    day = local_day(schedule, now)
    if state.day != day:
        state.day = day
        state.requests_today = 0
    return state


def after_clean(state: PaceState) -> PaceState:
    """A clean response. Every CLEAN_STREAK_FOR_RAISE of them raises the rate 10 percent."""
    state.clean_streak += 1
    if state.clean_streak >= CLEAN_STREAK_FOR_RAISE:
        state.factor = max(1.0, state.factor / RATE_RAISE)
        state.clean_streak = 0
    return state


def after_throttle(state: PaceState, now: datetime) -> PaceState:
    """429, 999 or a throttled empty page: halve the rate and cool down."""
    state.factor = min(state.factor * 2.0, MAX_FACTOR)
    state.clean_streak = 0
    state.cooldown_until = now + timedelta(seconds=COOLDOWN_SECONDS)
    state.calm_since = now
    return state


def after_repeat_throttle(state: PaceState, now: datetime) -> PaceState:
    """A retry of an already-throttled logical request was refused again: keep the
    cooldown and restart the calm clock, but do not double the spacing a second time."""
    state.clean_streak = 0
    state.cooldown_until = now + timedelta(seconds=COOLDOWN_SECONDS)
    state.calm_since = now
    return state


def after_calm(state: PaceState, now: datetime) -> PaceState:
    """Halve the spacing for every full CALM_HALVING_SECONDS since the last refusal."""
    if state.calm_since is None or state.factor <= 1.0:
        state.factor = max(state.factor, 1.0)
        state.calm_since = now
        return state
    periods = int((now - state.calm_since).total_seconds() // CALM_HALVING_SECONDS)
    if periods > 0:
        state.factor = max(1.0, state.factor / 2.0 ** periods)
        state.calm_since += timedelta(seconds=periods * CALM_HALVING_SECONDS)
    return state


def update_breaker(state: PaceState, outcome: str, now: datetime,
                   schedule: ScheduleConfig) -> PaceState:
    """Feed one request outcome into the circuit breaker.

    `rest_level == 0` means normal running: the first refusal of a logical request
    is counted, and `breaker_refusals` of them inside `breaker_window_minutes`
    start a rest at the bottom of the ladder. `rest_level > 0` means a rest has
    ended and the request was the probe: a refusal escalates one rung, anything
    not refused resets the ladder. Repeat refusals of the same logical request do
    not count, matching the AIMD rule.
    """
    ladder = schedule.rest_ladder_hours
    if outcome == "throttled":
        if state.rest_level > 0:
            state.rest_level += 1
            hours = ladder[min(state.rest_level - 1, len(ladder) - 1)]
            state.rest_until = now + timedelta(hours=hours)
            state.refusal_times = []
        else:
            cutoff = now - timedelta(minutes=schedule.breaker_window_minutes)
            state.refusal_times = [t for t in state.refusal_times if t >= cutoff]
            state.refusal_times.append(now)
            if len(state.refusal_times) >= schedule.breaker_refusals:
                state.rest_until = now + timedelta(hours=ladder[0])
                state.rest_level = 1
                state.refusal_times = []
    elif outcome in ("clean", "neutral") and state.rest_level > 0 and (
            state.rest_until is None or state.rest_until <= now):
        # A probe that LinkedIn did not refuse: the rest worked, start clean.
        state.rest_level = 0
        state.rest_until = None
        state.refusal_times = []
    return state


def spacing_seconds(factor: float, uniform=random.uniform) -> float:
    low, high = BASE_SPACING_RANGE
    return min(uniform(low, high) * factor, FLOOR_SPACING_SECONDS)


def _lock_key(host: str) -> int:
    return zlib.crc32(f"jds-pace:{host}".encode())


class Slot:
    """The caller's handle on one granted request turn."""

    def __init__(self):
        self.outcome = "neutral"
        self.status: int | None = None

    def clean(self, status: int | None = None) -> None:
        self.outcome = "clean"
        self.status = status

    def throttled(self, status: int | None = None) -> None:
        self.outcome = "throttled"
        self.status = status

    def throttled_again(self, status: int | None = None) -> None:
        """The retry of a request that was already throttled was refused too."""
        self.outcome = "throttled_again"
        self.status = status

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
                 schedule: ScheduleConfig | None = None):
        self.conn = conn
        self.host = host
        self.key = _lock_key(host)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep
        self.uniform = uniform
        self.service = service
        self.schedule = schedule or ScheduleConfig()

    # -- storage ---------------------------------------------------------
    def _load(self) -> PaceState:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pace_budgets (host, next_slot_at) VALUES (%s, %s) "
                "ON CONFLICT (host) DO NOTHING", (self.host, self.clock()))
            cur.execute(
                "SELECT next_slot_at, factor, clean_streak, cooldown_until, last_kind, wanting, "
                "calm_since, requests_today, day, refusal_times, rest_until, rest_level "
                "FROM pace_budgets WHERE host = %s", (self.host,))
            (next_slot, factor, streak, cooldown, last_kind, wanting, calm_since,
             requests_today, day, refusal_times, rest_until, rest_level) = cur.fetchone()
        state = PaceState(next_slot, factor, streak, cooldown, last_kind, dict(wanting or {}),
                          calm_since, requests_today, day, list(refusal_times or []),
                          rest_until, rest_level)
        return roll_day(state, self.schedule, self.clock())

    def _save(self, state: PaceState) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE pace_budgets SET next_slot_at = %s, factor = %s, clean_streak = %s, "
                "cooldown_until = %s, last_kind = %s, calm_since = %s, wanting = wanting - %s, "
                "requests_today = %s, day = %s, refusal_times = %s, rest_until = %s, rest_level = %s "
                "WHERE host = %s",
                (state.next_slot_at, state.factor, state.clean_streak, state.cooldown_until,
                 state.last_kind, state.calm_since, state.last_kind, state.requests_today,
                 state.day, state.refusal_times, state.rest_until, state.rest_level, self.host))

    def _declare_wanting(self, kind: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pace_budgets (host, next_slot_at, wanting) VALUES (%s, %s, %s::jsonb) "
                "ON CONFLICT (host) DO UPDATE SET wanting = pace_budgets.wanting || %s::jsonb",
                (self.host, self.clock(), json.dumps({kind: self.clock().timestamp()}),
                 json.dumps({kind: self.clock().timestamp()})))

    def _try_lock(self) -> bool:
        with self.conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (self.key,))
            return cur.fetchone()[0]

    def _unlock(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (self.key,))

    def snapshot(self) -> PaceState:
        """Current stored state, for status displays. Takes no lock."""
        return after_calm(self._load(), self.clock())

    def gate(self) -> Gate:
        """Why LinkedIn is open or closed right now. Takes no lock and no request."""
        state = self._load()
        return evaluate_gate(self.schedule, self.clock(), requests_today=state.requests_today,
                             rest_until=state.rest_until, rest_level=state.rest_level)

    def seconds_until_slot(self) -> float:
        """How long a request made now would wait for its turn, ignoring the other
        service's place in the queue. Takes no lock."""
        state = self._load()
        ready = max(state.next_slot_at, state.cooldown_until or state.next_slot_at)
        return max(0.0, (ready - self.clock()).total_seconds())

    # -- the turn --------------------------------------------------------
    def _other_is_waiting(self, state: PaceState, kind: str) -> bool:
        now = self.clock().timestamp()
        return any(
            other != kind and now - stamp <= WANTING_FRESH_SECONDS
            for other, stamp in state.wanting.items()
        )

    def _acquire(self, kind: str) -> PaceState:
        while True:
            self._declare_wanting(kind)
            if not self._try_lock():
                self.sleep(LOCK_POLL_SECONDS)
                continue
            state = after_calm(self._load(), self.clock())
            if state.last_kind == kind and self._other_is_waiting(state, kind):
                self._unlock()
                self.sleep(YIELD_SECONDS)
                continue
            return state

    def _wait_for_slot(self, state: PaceState) -> None:
        ready = state.next_slot_at
        if state.cooldown_until and state.cooldown_until > ready:
            ready = state.cooldown_until
        delay = (ready - self.clock()).total_seconds()
        if delay > 0:
            self.sleep(delay)

    def _record(self, state: PaceState, kind: str, slot: Slot, now: datetime) -> None:
        """Count the request against the Rome day, feed the breaker, and log it."""
        roll_day(state, self.schedule, now)
        state.requests_today += 1
        update_breaker(state, slot.outcome, now, self.schedule)
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pace_events (at, service, kind, outcome, status, factor, requests_today) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (now, self.service, kind, EVENT_OUTCOMES.get(slot.outcome, slot.outcome),
                 slot.status, state.factor, state.requests_today))

    @contextmanager
    def slot(self, kind: str):
        state = self._acquire(kind)
        now = self.clock()
        gate = evaluate_gate(self.schedule, now, requests_today=state.requests_today,
                             rest_until=state.rest_until, rest_level=state.rest_level)
        if not gate.open:
            state.last_kind = kind
            state.wanting.pop(kind, None)
            self._save(state)
            self._unlock()
            raise PaceGateClosed(gate)
        slot = Slot()
        try:
            self._wait_for_slot(state)
            yield slot
        finally:
            try:
                now = self.clock()
                if slot.outcome == "clean":
                    after_clean(state)
                elif slot.outcome == "throttled":
                    after_throttle(state, now)
                    print(f"[pace] LinkedIn throttled a {kind} request: spacing now "
                          f"x{state.factor:g}, cooling down {COOLDOWN_SECONDS:g}s")
                elif slot.outcome == "throttled_again":
                    after_repeat_throttle(state, now)
                self._record(state, kind, slot, now)
                state.last_kind = kind
                state.wanting.pop(kind, None)
                state.next_slot_at = now + timedelta(
                    seconds=spacing_seconds(state.factor, self.uniform))
                self._save(state)
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
    def slot(self, kind: str):
        with self._current().slot(kind) as slot:
            yield slot
