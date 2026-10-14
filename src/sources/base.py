"""Shared plumbing for the stage-2 source collectors (freehire.me, Himalayas):
a polite JSON client, the slice bookkeeping, and the result type.

These sources are documented public APIs, so there is no pace budget shared with
LinkedIn; each client keeps its own minimum gap between requests, honours the
rate-limit headers the API sends, and identifies itself with a User-Agent.
"""
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import requests

from src.jobstore.sightings import CardInput, record_sighting

RETRY_WAITS = (5, 15, 45)


class SourceError(RuntimeError):
    """The API failed in a way retries did not fix."""


class SourceThrottled(SourceError):
    """The API kept answering 429."""


@dataclass(frozen=True)
class SourceConfig:
    enabled: bool
    base_url: str
    user_agent: str
    min_interval_seconds: float
    interval_hours: int
    options: dict


def load_source_config(name: str, path: str = "config/sources.json") -> SourceConfig:
    with open(path) as handle:
        data = json.load(handle)[name]
    interval_hours = int(data["interval_hours"])
    if interval_hours < 2:
        # A walk schedules the next run a little short of a full interval; an interval of an
        # hour or less would leave a finished slice due again in the same oneshot drain.
        raise ValueError(f"{name}: interval_hours must be at least 2, got {interval_hours}")
    shared = {"enabled", "base_url", "user_agent", "min_interval_seconds", "interval_hours"}
    return SourceConfig(
        enabled=bool(data["enabled"]), base_url=data["base_url"], user_agent=data["user_agent"],
        min_interval_seconds=float(data["min_interval_seconds"]), interval_hours=interval_hours,
        options={key: value for key, value in data.items() if key not in shared})


class JsonClient:
    def __init__(self, cfg: SourceConfig, *, get=requests.get, sleep=time.sleep, clock=time.monotonic):
        self.cfg, self._get, self._sleep, self._clock = cfg, get, sleep, clock
        self._last: float | None = None

    def _pace(self) -> None:
        if self._last is not None:
            wait = self.cfg.min_interval_seconds - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)

    def get_json(self, path: str, params) -> dict:
        url = self.cfg.base_url.rstrip("/") + path
        headers = {"User-Agent": self.cfg.user_agent, "Accept": "application/json"}
        problem = "no attempt"
        for attempt in range(len(RETRY_WAITS) + 1):
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
                    if remaining is not None and remaining.isdigit() and int(remaining) <= 2:
                        reset = response.headers.get("X-RateLimit-Reset", "5")
                        self._sleep(int(reset) + 1 if reset.isdigit() else 5)
                    try:
                        return response.json()
                    except ValueError:
                        # A 200 with an HTML error page (proxy, maintenance) is not JSON;
                        # retry it like a 5xx instead of letting it abort the whole drain.
                        problem = "invalid JSON"
                        if attempt < len(RETRY_WAITS):
                            self._sleep(RETRY_WAITS[attempt])
                        continue
                if status == 429:
                    retry_after = response.headers.get("Retry-After", "")
                    self._sleep(int(retry_after) if retry_after.isdigit() else RETRY_WAITS[min(attempt, 2)])
                    problem = "HTTP 429"
                    continue
                if status >= 500:
                    problem = f"HTTP {status}"
                else:
                    raise SourceError(f"{url} returned HTTP {status}")
            if attempt < len(RETRY_WAITS):
                self._sleep(RETRY_WAITS[attempt])
        raise (SourceThrottled if problem == "HTTP 429" else SourceError)(f"{url}: {problem}")


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
