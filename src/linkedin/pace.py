"""Shared LinkedIn pace budget (spec sections 4.2 and 4.4).

One budget per host, stored in the `pace_budgets` table and guarded by a
Postgres session advisory lock, so the collector and the processor cannot
exceed it together and never have two LinkedIn requests in flight.

Two things gate every request, all enforced here so no caller can bypass them:

* the quiet window (config/schedule.json `closed_hours`, Rome time);
* the shared daily request budget (weekday/weekend cap, Rome calendar day,
  persisted as `requests_today`/`day`).

There is no circuit breaker: a refusal is friction the steady pace waits out,
not a reason to shut LinkedIn down for hours. When the gate is closed `slot`
raises PaceGateClosed instead of blocking a tick for hours; the services catch
that and go idle. Every request is recorded in `pace_events`.

This module also holds the pure steady-pace policy: the base spacing per window
(75 s weekday / 110 s weekend, never below the cap-derived floor), the jitter,
the day verdict that tightens after three calm days and loosens after a bad one,
the refusal wait (60 s doubling, capped at 30 min) and the hour-level nudge.
"""
import json
import random
import time
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

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
    reason: str  # open | closed_hours | daily_cap
    requests_today: int
    cap: int
    window: str  # weekday | weekend


class PaceGateClosed(RuntimeError):
    """The schedule gate says no LinkedIn request may be made now. `gate` says why."""

    def __init__(self, gate: Gate):
        super().__init__(f"LinkedIn gate closed ({gate.reason})")
        self.gate = gate


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
    factor: float = 1.0
    clean_streak: int = 0
    cooldown_until: datetime | None = None
    last_kind: str | None = None
    wanting: dict[str, float] = field(default_factory=dict)
    calm_since: datetime | None = None
    requests_today: int = 0
    day: date | None = None
    refusal_times: list[datetime] = field(default_factory=list)
    refusals_today: int = 0
    refusal_streak: int = 0
    request_times: list[datetime] = field(default_factory=list)
    nudge: float = 1.0
    nudged_at: datetime | None = None
    weekday_spacing: float | None = None
    weekend_spacing: float | None = None
    calm_days: int = 0
    last_clean_at: datetime | None = None


def _set_stored_spacing(state: PaceState, window: str, value: float) -> None:
    if window == "weekend":
        state.weekend_spacing = value
    else:
        state.weekday_spacing = value


def stored_spacing(state: PaceState, schedule: ScheduleConfig, window: str) -> float:
    """The current base spacing for one window, from its stored value or the config
    start, clamped to [floor, start * max_spacing_factor]."""
    raw = state.weekend_spacing if window == "weekend" else state.weekday_spacing
    if raw is None:
        return start_spacing_seconds(schedule, window)
    floor = floor_spacing_seconds(schedule, window)
    ceiling = start_spacing_seconds(schedule, window) * schedule.pace.max_spacing_factor
    return max(floor, min(raw, ceiling))


def base_spacing(state: PaceState, schedule: ScheduleConfig, now: datetime) -> float:
    """The stored base spacing for `now`'s window."""
    return stored_spacing(state, schedule, window_name(schedule, now))


def next_spacing(state: PaceState, schedule: ScheduleConfig, now: datetime,
                 uniform=random.uniform) -> float:
    """The next sleep: the base spacing, scaled by the hour nudge, jittered by
    +/-`pace.jitter`, never below the cap-derived floor."""
    window = window_name(schedule, now)
    base = stored_spacing(state, schedule, window)
    factor = uniform(1 - schedule.pace.jitter, 1 + schedule.pace.jitter)
    return max(floor_spacing_seconds(schedule, window), base * state.nudge * factor)


def refusal_wait_seconds(streak: int, retry_after: float | None, policy: RefusalPolicy,
                         uniform=random.uniform, *, jitter: float) -> float:
    """How long to wait after the `streak`-th consecutive refusal. `Retry-After`
    wins when present; otherwise the first wait doubles per refusal. Both are capped
    at `max_wait_seconds` (the anti-shutdown guarantee)."""
    if retry_after is not None:
        return min(retry_after, policy.max_wait_seconds)
    base = policy.first_wait_seconds * 2 ** max(streak - 1, 0)
    return min(base * uniform(1 - jitter, 1 + jitter), policy.max_wait_seconds)


def update_nudge(state: PaceState, now: datetime, policy: RefusalPolicy) -> None:
    """Roll the trailing-hour window and nudge the spacing up when the last hour's
    refusals passed `hour_refusal_rate` (at most once an hour, capped)."""
    cutoff = now - timedelta(hours=1)
    state.request_times = [t for t in state.request_times if t > cutoff]
    state.refusal_times = [t for t in state.refusal_times if t > cutoff]
    if not state.refusal_times:
        state.nudge = 1.0
        return
    rate = len(state.refusal_times) / max(len(state.request_times), 1)
    due = state.nudged_at is None or now - state.nudged_at >= timedelta(hours=1)
    if rate > policy.hour_refusal_rate and due:
        state.nudge = min(state.nudge * policy.hour_multiplier, policy.hour_max_multiplier)
        state.nudged_at = now


def judge_day(state: PaceState, schedule: ScheduleConfig) -> None:
    """Feed one finished open day into the pace: three calm days tighten both bases,
    a day over the high refusal rate loosens them, anything between breaks the calm
    streak. A day with too few requests is no evidence and gives no verdict."""
    if state.requests_today < schedule.pace.min_day_requests:
        return
    rate = state.refusals_today / state.requests_today
    if rate < schedule.pace.calm_refusal_rate:
        state.calm_days += 1
        if state.calm_days >= schedule.pace.calm_days_to_tighten:
            for window in ("weekday", "weekend"):
                floor = floor_spacing_seconds(schedule, window)
                current = stored_spacing(state, schedule, window)
                _set_stored_spacing(state, window, max(floor, current * (1 - schedule.pace.step)))
            state.calm_days = 0
    elif rate > schedule.pace.loosen_refusal_rate:
        for window in ("weekday", "weekend"):
            current = stored_spacing(state, schedule, window)
            ceiling = start_spacing_seconds(schedule, window) * schedule.pace.max_spacing_factor
            _set_stored_spacing(state, window, min(ceiling, current * (1 + schedule.pace.step)))
        state.calm_days = 0
    else:
        state.calm_days = 0


def roll_day(state: PaceState, schedule: ScheduleConfig, now: datetime) -> PaceState:
    """Judge the stored day once when the Rome calendar day has moved on, then set
    the new day and zero the counters. A multi-day gap is judged once, never once
    per missed day."""
    day = local_day(schedule, now)
    if state.day is not None and state.day != day:
        judge_day(state, schedule)
    if state.day != day:
        state.day = day
        state.requests_today = 0
        state.refusals_today = 0
    return state


def _fresh(state: PaceState, marker: str, now_ts: float) -> bool:
    stamp = state.wanting.get(marker)
    return stamp is not None and now_ts - stamp <= WANTING_FRESH_SECONDS


def must_yield(state: PaceState, kind: str, overdue: bool, now_ts: float) -> bool:
    """Searches first (design section 4.3): a description yields while a search is
    waiting, unless it is overdue and the last turn was a search (then it takes
    every second turn). A search yields only to a waiting, overdue description."""
    search_waiting = _fresh(state, "search", now_ts)
    description_waiting = _fresh(state, "description", now_ts)
    overdue_waiting = _fresh(state, "overdue", now_ts)
    if kind == "description":
        if not search_waiting:
            return False
        return not (overdue and state.last_kind == "search")
    if kind == "search":
        return description_waiting and overdue_waiting and state.last_kind == "search"
    return False


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
        self.status: int | None = None
        self.retry_after: float | None = None

    def clean(self, status: int | None = None) -> None:
        self.outcome = "clean"
        self.status = status

    def throttled(self, status: int | None = None, retry_after: float | None = None) -> None:
        self.outcome = "throttled"
        self.status = status
        self.retry_after = retry_after

    def throttled_again(self, status: int | None = None, retry_after: float | None = None) -> None:
        """The retry of a request that was already throttled was refused too."""
        self.outcome = "throttled_again"
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
                "calm_since, requests_today, day, refusal_times "
                "FROM pace_budgets WHERE host = %s", (self.host,))
            (next_slot, factor, streak, cooldown, last_kind, wanting, calm_since,
             requests_today, day, refusal_times) = cur.fetchone()
        state = PaceState(next_slot, factor, streak, cooldown, last_kind, dict(wanting or {}),
                          calm_since, requests_today, day, list(refusal_times or []))
        return roll_day(state, self.schedule, self.clock())

    def _save(self, state: PaceState) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE pace_budgets SET next_slot_at = %s, factor = %s, clean_streak = %s, "
                "cooldown_until = %s, last_kind = %s, calm_since = %s, wanting = wanting - %s, "
                "requests_today = %s, day = %s, refusal_times = %s "
                "WHERE host = %s",
                (state.next_slot_at, state.factor, state.clean_streak, state.cooldown_until,
                 state.last_kind, state.calm_since, state.last_kind, state.requests_today,
                 state.day, state.refusal_times, self.host))

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
        return evaluate_gate(self.schedule, self.clock(), requests_today=state.requests_today)

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

    def _record(self, state: PaceState, slot: Slot, now: datetime) -> None:
        """Count the request against the Rome day. The caller saves the row first; the
        pace_events row is written afterwards, so a logging failure can never lose the
        count."""
        roll_day(state, self.schedule, now)
        state.requests_today += 1

    def _log_event(self, state: PaceState, kind: str, slot: Slot, now: datetime) -> None:
        """Append one request to pace_events. Telemetry must never break a run, so a
        failed insert is logged and swallowed; the state is already saved."""
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO pace_events (at, service, kind, outcome, status, factor, requests_today) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (now, self.service, kind, EVENT_OUTCOMES.get(slot.outcome, slot.outcome),
                     slot.status, state.factor, state.requests_today))
        except Exception as exc:  # noqa: BLE001 - telemetry must not raise into the pipeline
            print(f"[pace] could not record the {kind} request in pace_events: "
                  f"{type(exc).__name__}: {exc}")

    def _gate(self, state: PaceState) -> Gate:
        return evaluate_gate(self.schedule, self.clock(), requests_today=state.requests_today)

    def _abandon(self, state: PaceState, kind: str) -> None:
        """Release a turn that will make no request: keep the loaded state, drop our
        wanting marker, and let the other service take the lock."""
        state.last_kind = kind
        state.wanting.pop(kind, None)
        self._save(state)
        self._unlock()

    @contextmanager
    def slot(self, kind: str, *, overdue: bool = False):
        state = self._acquire(kind)
        gate = self._gate(state)
        if gate.open:
            try:
                self._wait_for_slot(state)
            except BaseException:
                self._abandon(state, kind)
                raise
            # The wait can last up to the floor spacing (300 s); a request granted just
            # before midnight must not fire inside the quiet window.
            gate = self._gate(state)
        if not gate.open:
            self._abandon(state, kind)
            raise PaceGateClosed(gate)
        slot = Slot()
        try:
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
                self._record(state, slot, now)
                state.last_kind = kind
                state.wanting.pop(kind, None)
                state.next_slot_at = now + timedelta(
                    seconds=spacing_seconds(state.factor, self.uniform))
                self._save(state)
                self._log_event(state, kind, slot, now)
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
