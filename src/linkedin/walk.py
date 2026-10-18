"""Walk one slice: page through its results and record every card as a sighting."""
import time
from dataclasses import dataclass
from datetime import datetime

import requests

from src.jobstore.sightings import CardInput, record_sighting
from src.linkedin.catalog import CollectorConfig, SOURCE
from src.linkedin.pace import PaceGateClosed
from src.linkedin.search import Page, SearchFailed, SearchThrottled, fetch_page
from src.linkedin.windows import (
    DEFAULT_MAX_INTERVAL, DEFAULT_MIN_INTERVAL, f_tpr, next_due, next_interval,
    window_seconds,
)
from src.services.pulse import pulse

# Stop reasons that leave the slice unfinished: it resumes from `next_start`.
UNFINISHED = frozenset({"throttled_empty", "rate_limited", "error"})
# The same set, for the resume timers that walk only unfinished slices.
RESUMABLE_STOPS = ("rate_limited", "error", "throttled_empty")


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
                    unfinished_only: bool = False) -> SliceRow | None:
    """The next due slice of `source`: the one due longest ago, whatever its tier, so
    no tier can be starved; slices due at the same instant are taken in tier order
    (tier 1 first). One collector per source, so no row lock. With `unfinished_only`,
    only slices whose last walk ended unfinished (the resume timers) are considered."""
    query = (
        "SELECT id, role, area, tier, base_interval_seconds, interval_seconds, last_walked_at, "
        "consecutive_empty, resume_start FROM slices "
        "WHERE active AND source = %s AND due_at <= %s")
    params: list = [source, now]
    if unfinished_only:
        query += " AND stop_reason = ANY(%s)"
        params.append(list(RESUMABLE_STOPS))
    query += " ORDER BY due_at, tier LIMIT 1"
    with conn.cursor() as cur:
        cur.execute(query, params)
        row = cur.fetchone()
    return SliceRow(*row) if row else None


def _is_exhausted(page: Page, walk_seen: set[str]) -> bool:
    """No cards, or only cards this walk already saw (LinkedIn repeats the last page)."""
    return not page.cards or all(card.job_id in walk_seen for card in page.cards)


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
    refusals = 0        # consecutive refusals in this walk
    distrust = False    # True right after a refusal: an empty 200 is not trusted

    while pages < cfg.max_pages_for(row.area):
        pulse({"slice": f"{row.role} / {row.area}", "pages": pages, "new_jobs": new_jobs})
        try:
            page = fetch_page(pace, role=row.role, area=row.area, f_tpr_value=f_tpr_value,
                              start=start, distrust_empty=distrust, get=get, sleep=sleep)
        except PaceGateClosed:
            stop_reason = "rate_limited"  # unfinished, resumes at `start`
            break
        except SearchThrottled:
            refusals += 1
            distrust = True
            if refusals >= cfg.refused_pages_to_defer:
                stop_reason = "rate_limited"
                break
            continue  # same `start`; the pace applies the host wait
        except SearchFailed:
            stop_reason = "error"
            break
        if page.throttled:  # an empty 200 right after a refusal
            refusals += 1
            distrust = True
            if refusals >= cfg.refused_pages_to_defer:
                stop_reason = "throttled_empty"
                break
            continue  # same `start`
        refusals = 0
        distrust = False
        pages += 1  # only responses that were not refusals count
        if page.end or _is_exhausted(page, walk_seen):
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
        if not last_page_full:
            stop_reason = "end_of_results"
        elif cfg.max_pages_for(row.area) < cfg.max_pages_per_slice:
            stop_reason = "area_cap"
        else:
            stop_reason = "cap_hit"

    return WalkResult(pages, cards_seen, new_jobs, stop_reason, window, start)


def finish_walk(conn, row: SliceRow, result: WalkResult, started_at: datetime,
                finished_at: datetime, cfg: CollectorConfig | None = None) -> None:
    """Persist the walk on the slice and schedule the next one. The caller commits.

    A walk that did not finish keeps last_walked_at, so the next window still
    reaches back to the last complete walk, and resumes at result.next_start.
    A finished walk is scheduled from its start (so its own duration does not drift
    the cadence) and an unfinished one from its finish (so a long deferred walk rests
    before it is retried). The 12-48 h bounds come from the collector config when one
    is given.
    """
    empty = result.finished and result.cards_seen == 0
    consecutive_empty = row.consecutive_empty + 1 if empty else (
        0 if result.cards_seen else row.consecutive_empty)
    interval = next_interval(
        base=row.base_interval_seconds, current=row.interval_seconds,
        stop_reason=result.stop_reason, consecutive_empty=consecutive_empty,
        min_interval=(cfg.min_interval_hours * 3600 if cfg else DEFAULT_MIN_INTERVAL),
        max_interval=(cfg.max_interval_hours * 3600 if cfg else DEFAULT_MAX_INTERVAL))
    due_from = started_at if result.finished else finished_at
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
                next_due(due_from, interval, result.stop_reason), row.id,
            ),
        )
