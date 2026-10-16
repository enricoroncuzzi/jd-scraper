"""Shared plumbing for the stage-2 source collectors (freehire.me, Himalayas):
a polite JSON client, the slice bookkeeping, and the result type.

These sources are documented public APIs, so there is no pace budget shared with
LinkedIn; each client keeps its own jittered minimum gap between requests,
honours the rate-limit headers the API sends, and identifies itself with a
User-Agent. One cookie-free session is reused for the client's whole life.

A 429 waits exactly `Retry-After` (or the configured fallback) and the same walk
carries on, bounded by a maximum single wait and a maximum total wait per run so
the unit's `TimeoutStartSec` is never exceeded; past either bound, or after
`retries_per_page` re-asks, the walk is left unfinished and resumes in 30 minutes.
"""
import json
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import requests

from src.http_session import new_session, retry_after_seconds
from src.jobstore.db import utcnow
from src.jobstore.sightings import CardInput, record_sighting

RETRY_WAITS = (5, 15, 45)


class SourceError(RuntimeError):
    """The API failed in a way retries did not fix."""


class SourceThrottled(SourceError):
    """The API kept answering 429 past the configured bounds."""


@dataclass(frozen=True)
class ThrottlePolicy:
    retries_per_page: int | None
    default_retry_after_seconds: float
    max_wait_seconds: float
    run_budget_seconds: float


@dataclass(frozen=True)
class SourceConfig:
    enabled: bool
    base_url: str
    user_agent: str
    spacing_seconds: tuple[float, float]
    interval_hours: int
    throttle: ThrottlePolicy
    options: dict


def load_source_config(name: str, path: str = "config/sources.json") -> SourceConfig:
    with open(path) as handle:
        data = json.load(handle)[name]
    interval_hours = int(data["interval_hours"])
    if interval_hours < 2:
        # A walk schedules the next run a little short of a full interval; an interval of an
        # hour or less would leave a finished slice due again in the same oneshot drain.
        raise ValueError(f"{name}: interval_hours must be at least 2, got {interval_hours}")
    spacing = data.get("spacing_seconds")
    if (not isinstance(spacing, list) or len(spacing) != 2
            or not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                       for value in spacing)
            or not 0 < spacing[0] <= spacing[1]):
        raise ValueError(f"{name}.spacing_seconds must be [low, high] with 0 < low <= high")
    throttle = data.get("throttle")
    if not isinstance(throttle, dict):
        raise ValueError(f"{name}.throttle must be an object")
    retries = throttle.get("retries_per_page")
    if retries is not None and (not isinstance(retries, int) or isinstance(retries, bool) or retries < 0):
        raise ValueError(f"{name}.throttle.retries_per_page must be a non-negative integer or null")
    policy = ThrottlePolicy(
        retries_per_page=retries,
        default_retry_after_seconds=float(throttle["default_retry_after_seconds"]),
        max_wait_seconds=float(throttle["max_wait_seconds"]),
        run_budget_seconds=float(throttle["run_budget_seconds"]),
    )
    shared = {"enabled", "base_url", "user_agent", "spacing_seconds", "interval_hours", "throttle"}
    return SourceConfig(
        enabled=bool(data["enabled"]), base_url=data["base_url"], user_agent=data["user_agent"],
        spacing_seconds=(float(spacing[0]), float(spacing[1])), interval_hours=interval_hours,
        throttle=policy, options={key: value for key, value in data.items() if key not in shared})


class JsonClient:
    def __init__(self, cfg: SourceConfig, *, get=None, sleep=time.sleep, clock=time.monotonic,
                 uniform=random.uniform):
        self.cfg, self._get, self._sleep, self._clock, self._uniform = \
            cfg, get or new_session().get, sleep, clock, uniform
        self._last: float | None = None
        self._started = clock()
        self._throttles = 0
        self._hold = 0.0

    def _pace(self) -> None:
        if self._last is not None:
            if self._hold - (self._clock() - self._last) > 0:
                self._bounded_wait(self._hold - (self._clock() - self._last))
            self._hold = 0.0
            wait = self._uniform(*self.cfg.spacing_seconds) - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)

    def _bounded_wait(self, wait: float) -> None:
        policy = self.cfg.throttle
        if wait > policy.max_wait_seconds \
                or self._clock() - self._started + wait > policy.run_budget_seconds:
            raise SourceThrottled(f"{self.cfg.base_url}: 429 wait {wait:g}s exceeds the run bound")
        self._sleep(wait)

    def get_json(self, path: str, params) -> dict:
        url = self.cfg.base_url.rstrip("/") + path
        headers = {"User-Agent": self.cfg.user_agent, "Accept": "application/json"}
        problem = "no attempt"
        attempts = 0
        self._throttles = 0  # 429 re-asks are counted per page, not per run
        while True:
            self._pace()
            try:
                response = self._get(url, params=params, headers=headers, timeout=30)
            except requests.RequestException as exc:
                problem = f"network error: {exc}"
                response = None
            self._last = self._clock()
            if response is not None:
                status = response.status_code
                if status == 200:
                    remaining = response.headers.get("X-RateLimit-Remaining")
                    if remaining is not None and remaining.isascii() and remaining.isdigit() \
                            and int(remaining) <= 2:
                        reset = response.headers.get("X-RateLimit-Reset", "5")
                        self._hold = int(reset) + 1 if reset.isascii() and reset.isdigit() else 5
                    try:
                        return response.json()
                    except ValueError:
                        # A 200 with an HTML error page (proxy, maintenance) is not JSON;
                        # retry it like a 5xx instead of letting it abort the whole drain.
                        problem = "invalid JSON"
                    # fall through to the neutral retry
                elif status == 429:
                    self._throttles += 1
                    if self.cfg.throttle.retries_per_page is not None \
                            and self._throttles > self.cfg.throttle.retries_per_page:
                        raise SourceThrottled(f"{url}: too many 429s")
                    parsed = retry_after_seconds(response.headers.get("Retry-After"), utcnow())
                    wait = (self.cfg.throttle.default_retry_after_seconds
                            if parsed is None else parsed)
                    self._bounded_wait(wait)
                    self._last = self._clock()
                    continue
                elif status >= 500:
                    problem = f"HTTP {status}"
                else:
                    raise SourceError(f"{url} returned HTTP {status}")
            if attempts >= len(RETRY_WAITS):
                break
            self._sleep(RETRY_WAITS[attempts])
            attempts += 1
        raise SourceError(f"{url}: {problem}")


@dataclass(frozen=True)
class SourceWalk:
    pages: int
    cards_seen: int
    new_jobs: int
    stop_reason: str
    next_offset: int = 0

    @property
    def finished(self) -> bool:
        return self.stop_reason not in ("rate_limited", "error")


def record_cards(conn, cards: list[CardInput], now: datetime) -> tuple[int, bool]:
    """Record a page of cards. Returns (new jobs, every card already known)."""
    new = 0
    for card in cards:
        if not record_sighting(conn, card, now).known:
            new += 1
    conn.commit()
    return new, bool(cards) and new == 0


def finish_source_walk(conn, row, walk: SourceWalk, started: datetime, finished: datetime,
                       interval_seconds: int) -> None:
    """Persist a daily source's walk. An unfinished walk resumes at next_offset in 30 minutes
    and keeps last_walked_at, so the next window still reaches back to the last complete walk."""
    due = finished + (timedelta(minutes=30) if not walk.finished else timedelta(seconds=interval_seconds))
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE slices SET last_walked_at = %s, pages_walked = %s, cards_seen = %s, stop_reason = %s, "
            "resume_start = %s, due_at = %s WHERE id = %s",
            (started if walk.finished else row.last_walked_at, walk.pages, walk.cards_seen,
             walk.stop_reason, 0 if walk.finished else walk.next_offset, due, row.id))
