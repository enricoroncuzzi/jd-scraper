"""Record that a source saw a job. Collectors call only this module."""
from dataclasses import dataclass
from datetime import datetime

from src.jobstore.db import utcnow
from src.jobstore.identity import is_company_page, normalize_company, normalize_title
from src.tier_scope import resolve_country


@dataclass(frozen=True)
class CardInput:
    """One job as a source reported it. Description fields are set only by
    sources that return the text inline (freehire, Himalayas); LinkedIn cards
    leave them None and the processor fetches the description later."""
    source: str
    source_job_id: str
    source_url: str
    title: str
    company: str
    location: str = "N/A"
    posted_at: datetime | None = None
    slice_id: int | None = None
    canonical_url: str | None = None
    country: str | None = None
    work_mode: str = ""
    remote_hint: str | None = None
    tier_hint: int | None = None
    description: str | None = None
    description_source: str | None = None
    source_detail: str | None = None  # where the source itself got the job (freehire: greenhouse, workday, ...)


@dataclass(frozen=True)
class SightingResult:
    job_id: str
    created: bool
    known: bool


def record_sighting(conn, card: CardInput, now: datetime | None = None) -> SightingResult:
    """Insert the sighting, and a `seen` job when this source job id is new.

    `known` is True when the (source, source_job_id) pair already existed: the
    sighting's last_seen_at and seen_count are refreshed and nothing else about
    the job changes (a re-sighting never re-verifies or re-scores).
    The caller commits.
    """
    now = now or utcnow()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, job_id FROM sightings WHERE source = %s AND source_job_id = %s FOR UPDATE",
            (card.source, card.source_job_id),
        )
        row = cur.fetchone()
        if row is not None:
            sighting_id, job_id = row
            cur.execute(
                "UPDATE sightings SET last_seen_at = %s, seen_count = seen_count + 1, "
                "source_url = %s WHERE id = %s",
                (now, card.source_url, sighting_id),
            )
            cur.execute("UPDATE jobs SET last_seen_at = %s WHERE id = %s", (now, job_id))
            return SightingResult(job_id=job_id, created=False, known=True)

        country = card.country or resolve_country(card.location)
        # A source's own page is the fallback; a tracking link from another board never is.
        canonical = (card.canonical_url if card.canonical_url and is_company_page(card.canonical_url)
                     else card.source_url)
        has_description = bool(card.description and card.description.strip())
        cur.execute(
            """
            INSERT INTO jobs (canonical_url, title, company, company_norm, title_norm, country,
                              location_raw, work_mode, remote_hint, tier_hint, posted_at,
                              first_seen_at, last_seen_at, stage_changed_at, next_attempt_at,
                              description, description_status, description_source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                canonical, card.title, card.company, normalize_company(card.company),
                normalize_title(card.title), country, card.location or "N/A", card.work_mode,
                card.remote_hint, card.tier_hint, card.posted_at, now, now, now, now,
                card.description if has_description else "",
                "ok" if has_description else "pending",
                card.description_source if has_description else None,
            ),
        )
        job_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO sightings (job_id, source, source_job_id, source_url, source_detail, seen_at, "
            "last_seen_at, slice_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (job_id, card.source, card.source_job_id, card.source_url, card.source_detail, now, now,
             card.slice_id),
        )
    return SightingResult(job_id=job_id, created=True, known=False)
