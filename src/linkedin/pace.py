"""Shared LinkedIn pace budget (spec section 4.2).

One budget per host, stored in the `pace_budgets` table and guarded by a
Postgres session advisory lock, so the collector and the processor cannot
exceed it together and never have two LinkedIn requests in flight.

The rate adapts AIMD-style: a throttle (429, 999, throttled empty page) doubles
the spacing and starts a 60 s cooldown. One logical request that is refused and
retried is one throttle: its repeat refusals keep the cooldown but do not double
again. The rate recovers two ways: every 50 clean responses it rises 10 percent,
and every 30 minutes without a refusal the spacing halves, back down to the base
rate. Search pages and description fetches take turns so neither starves the other.
"""
import json
import random
import time
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

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


@dataclass
class PaceState:
    next_slot_at: datetime
    factor: float = 1.0
    clean_streak: int = 0
    cooldown_until: datetime | None = None
    last_kind: str | None = None
    wanting: dict[str, float] = field(default_factory=dict)
    calm_since: datetime | None = None


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


def spacing_seconds(factor: float, uniform=random.uniform) -> float:
    low, high = BASE_SPACING_RANGE
    return min(uniform(low, high) * factor, FLOOR_SPACING_SECONDS)


def _lock_key(host: str) -> int:
    return zlib.crc32(f"jds-pace:{host}".encode())


class Slot:
    """The caller's handle on one granted request turn."""

    def __init__(self):
        self.outcome = "neutral"

    def clean(self) -> None:
        self.outcome = "clean"

    def throttled(self) -> None:
        self.outcome = "throttled"

    def throttled_again(self) -> None:
        """The retry of a request that was already throttled was refused too."""
        self.outcome = "throttled_again"

    def neutral(self) -> None:
        self.outcome = "neutral"


class PaceBudget:
    """Use `with pace.slot("search") as slot:` around exactly one LinkedIn request
    and call slot.clean() or slot.throttled() with what came back.

    `conn` must be a dedicated autocommit connection: the advisory lock lives as
    long as the session, so a crashed service releases it by disconnecting.
    """

    def __init__(self, conn, host: str = HOST, *, clock=None, sleep=time.sleep,
                 uniform=random.uniform):
        self.conn = conn
        self.host = host
        self.key = _lock_key(host)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep
        self.uniform = uniform

    # -- storage ---------------------------------------------------------
    def _load(self) -> PaceState:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pace_budgets (host, next_slot_at) VALUES (%s, %s) "
                "ON CONFLICT (host) DO NOTHING", (self.host, self.clock()))
            cur.execute(
                "SELECT next_slot_at, factor, clean_streak, cooldown_until, last_kind, wanting, calm_since "
                "FROM pace_budgets WHERE host = %s", (self.host,))
            next_slot, factor, streak, cooldown, last_kind, wanting, calm_since = cur.fetchone()
        return PaceState(next_slot, factor, streak, cooldown, last_kind, dict(wanting or {}), calm_since)

    def _save(self, state: PaceState) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE pace_budgets SET next_slot_at = %s, factor = %s, clean_streak = %s, "
                "cooldown_until = %s, last_kind = %s, calm_since = %s, wanting = wanting - %s "
                "WHERE host = %s",
                (state.next_slot_at, state.factor, state.clean_streak, state.cooldown_until,
                 state.last_kind, state.calm_since, state.last_kind, self.host))

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

    @contextmanager
    def slot(self, kind: str):
        state = self._acquire(kind)
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

    def __init__(self, connect, **budget_options):
        self._connect = connect
        self._options = budget_options
        self._budget: PaceBudget | None = None

    def _current(self) -> PaceBudget:
        if self._budget is None or self._budget.conn.closed:
            conn = self._connect()
            conn.autocommit = True
            self._budget = PaceBudget(conn, **self._options)
        return self._budget

    def seconds_until_slot(self) -> float:
        return self._current().seconds_until_slot()

    @contextmanager
    def slot(self, kind: str):
        with self._current().slot(kind) as slot:
            yield slot
