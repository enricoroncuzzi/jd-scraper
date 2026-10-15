"""Walk one slice: page through its results and record every card as a sighting."""
import time
from dataclasses import dataclass
from datetime import datetime

import requests

from src.jobstore.sightings import CardInput, record_sighting
from src.linkedin.catalog import CollectorConfig, SOURCE
from src.linkedin.pace import PaceGateClosed
from src.linkedin.search import (
    RETRY_WAITS, Page, SearchFailed, SearchThrottled, fetch_page,
)
from src.linkedin.windows import f_tpr, next_due, next_interval, window_seconds
from src.services.pulse import pulse

# Stop reasons that leave the slice unfinished: it resumes from `next_start`.
UNFINISHED = frozenset({"throttled_empty", "rate_limited", "error"})


@dataclass(frozen=True)
class SliceRow:
    id: int
    role: str
    area: str
    tier: int
    base_interval_seconds: int
    interval_seconds: int
    last_walked_at: datetime | None
    consecutive_empty: int
    resume_start: int


@dataclass(frozen=True)
class WalkResult:
    pages: int
    cards_seen: int
    new_jobs: int
    stop_reason: str
    window: int
    next_start: int

    @property
    def finished(self) -> bool:
        return self.stop_reason not in UNFINISHED


def claim_due_slice(conn, now: datetime, source: str = SOURCE, *,
                    taper_below_seconds: int | None = None) -> SliceRow | None:
    """The next due slice of `source`: tier 1 first, oldest due first. One collector per
    source, so no row lock.

    `taper_below_seconds` is the weekend taper: when set, a slice whose *base* cadence is
    that long or longer is skipped (its window simply grows, nothing is lost)."""
    query = (
        "SELECT id, role, area, tier, base_interval_seconds, interval_seconds, last_walked_at, "
        "consecutive_empty, resume_start FROM slices "
        "WHERE active AND source = %s AND due_at <= %s")
    params: list = [source, now]
    if taper_below_seconds is not None:
        query += " AND base_interval_seconds < %s"
        params.append(taper_below_seconds)
    query += " ORDER BY tier, due_at LIMIT 1"
    with conn.cursor() as cur:
        cur.execute(query, params)
        row = cur.fetchone()
    return SliceRow(*row) if row else None


def _is_exhausted(page: Page, walk_seen: set[str]) -> bool:
    """No cards, or only cards this walk already saw (LinkedIn repeats the last page)."""
    return not page.cards or all(card.job_id in walk_seen for card in page.cards)


def _reask_throttled_page(pace, row: SliceRow, f_tpr_value: str, start: int, walk_seen: set[str],
                          get, sleep) -> Page | None:
    """An empty page that arrived on a request that saw a throttle is not trusted
    as the end of the results. Ask for the same offset up to three more times."""
    for wait in RETRY_WAITS:
        sleep(wait)
        try:
            page = fetch_page(pace, role=row.role, area=row.area, f_tpr_value=f_tpr_value,
                              start=start, get=get, sleep=sleep)
        except PaceGateClosed:
            return None
        except (SearchThrottled, SearchFailed):
            continue
        if not _is_exhausted(page, walk_seen) or (page.end and not page.throttled):
            return page
    return None


def walk_slice(conn, pace, row: SliceRow, cfg: CollectorConfig, now: datetime, *,
               get=requests.get, sleep=time.sleep) -> WalkResult:
    window = window_seconds(now, row.last_walked_at, margin=cfg.window_margin_hours * 3600)
    f_tpr_value = f_tpr(window)
    start = row.resume_start
    walk_seen: set[str] = set()
    widest = 0
    known_streak = 0
    pages = cards_seen = new_jobs = 0
    stop_reason = "cap_hit"
    last_page_full = False

    for _ in range(cfg.max_pages_per_slice):
        pulse({"slice": f"{row.role} / {row.area}", "pages": pages, "new_jobs": new_jobs})
        try:
            page = fetch_page(pace, role=row.role, area=row.area, f_tpr_value=f_tpr_value,
                              start=start, get=get, sleep=sleep)
        except (SearchThrottled, PaceGateClosed):
            stop_reason = "rate_limited"
            break
        except SearchFailed:
            stop_reason = "error"
            break
        pages += 1
        if page.end:
            stop_reason = "end_of_results"
            break
        if _is_exhausted(page, walk_seen) and page.throttled:
            recovered = _reask_throttled_page(pace, row, f_tpr_value, start, walk_seen, get, sleep)
            if recovered is None:
                stop_reason = "throttled_empty"
                break
            page = recovered
            if page.end:
                stop_reason = "end_of_results"
                break
        if _is_exhausted(page, walk_seen):
            stop_reason = "end_of_results"
            break

        page_known = True
        for card in page.cards:
            result = record_sighting(conn, CardInput(
                source=SOURCE, source_job_id=card.job_id, source_url=card.url, title=card.title,
                company=card.company, location=card.location, posted_at=card.posted_at,
                slice_id=row.id, tier_hint=row.tier), now)
            walk_seen.add(card.job_id)
            if not result.known:
                page_known = False
                new_jobs += 1
        # Persist how far the walk got in the same transaction as its sightings, so a
        # hard stop (deploy kill, OOM, DB error) resumes here instead of at offset 0.
        with conn.cursor() as cur:
            cur.execute("UPDATE slices SET resume_start = %s WHERE id = %s",
                        (start + len(page.cards), row.id))
        conn.commit()

        cards_seen += len(page.cards)
        last_page_full = len(page.cards) >= widest
        widest = max(widest, len(page.cards))
        start += len(page.cards)
        known_streak = known_streak + 1 if (page_known and last_page_full) else 0
        if known_streak >= cfg.known_pages_to_stop:
            stop_reason = "known_page"
            break
    else:
        stop_reason = "cap_hit" if last_page_full else "end_of_results"

    return WalkResult(pages, cards_seen, new_jobs, stop_reason, window, start)


def finish_walk(conn, row: SliceRow, result: WalkResult, started_at: datetime,
                finished_at: datetime) -> None:
    """Persist the walk on the slice and schedule the next one. The caller commits.

    A walk that did not finish keeps last_walked_at, so the next window still
    reaches back to the last complete walk, and resumes at result.next_start.
    """
    empty = result.finished and result.cards_seen == 0
    consecutive_empty = row.consecutive_empty + 1 if empty else (
        0 if result.cards_seen else row.consecutive_empty)
    interval = next_interval(
        base=row.base_interval_seconds, current=row.interval_seconds,
        stop_reason=result.stop_reason, consecutive_empty=consecutive_empty)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO slice_walks (slice_id, walked_at, pages, cards_seen, new_jobs, stop_reason, "
            "window_seconds) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (row.id, started_at, result.pages, result.cards_seen, result.new_jobs, result.stop_reason,
             result.window))
        cur.execute(
            "UPDATE slices SET last_walked_at = %s, last_window = %s, pages_walked = %s, "
            "cards_seen = %s, stop_reason = %s, consecutive_empty = %s, interval_seconds = %s, "
            "resume_start = %s, due_at = %s WHERE id = %s",
            (
                started_at if result.finished else row.last_walked_at,
                result.window, result.pages, result.cards_seen, result.stop_reason,
                consecutive_empty, interval, 0 if result.finished else result.next_start,
                next_due(finished_at, interval, result.stop_reason), row.id,
            ),
        )
