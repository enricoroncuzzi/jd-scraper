"""Delivery's view of a job: the columns the digest, notes and messages need,
plus which sources saw it. Also the adapter to the ScoredOffer the existing
src/writer.py and src/telegram.py formatters take."""
from dataclasses import dataclass, field
from datetime import date, datetime

from src.models import ScoredOffer

_COLUMNS = (
    "id", "note_no", "title", "company", "location_raw", "canonical_url", "description",
    "description_status", "work_mode", "remote_verdict", "remote_reason", "score", "comment",
    "summary", "tier", "note_day", "alerted_at", "stage", "stage_changed_at",
)
SELECT_DELIVERY_JOB = f"SELECT {', '.join('j.' + c for c in _COLUMNS)} FROM jobs j"


@dataclass
class DeliveryJob:
    id: str
    note_no: int
    title: str
    company: str
    location_raw: str
    canonical_url: str
    description: str
    description_status: str
    work_mode: str
    remote_verdict: str | None
    remote_reason: str
    score: int | None
    comment: str
    summary: str
    tier: int
    note_day: date | None
    alerted_at: datetime | None
    stage: str = "scored"
    stage_changed_at: datetime | None = None
    source: str = "linkedin"
    also_seen_on: list[str] = field(default_factory=list)


def primary_source(canonical_url: str, sightings: list[tuple[str, str]]) -> tuple[str, list[str]]:
    """(source, also_seen_on) from [(source, source_url)] in first-seen order. The
    primary source is the one whose URL is the canonical link, else the first."""
    primary = next((s for s, url in sightings if url == canonical_url), None)
    if primary is None:
        primary = sightings[0][0] if sightings else "linkedin"
    others = []
    for source, _url in sightings:
        if source != primary and source not in others:
            others.append(source)
    return primary, others


def attach_sources(cur, jobs: list[DeliveryJob]) -> list[DeliveryJob]:
    if not jobs:
        return jobs
    cur.execute(
        "SELECT job_id, source, source_url FROM sightings WHERE job_id = ANY(%s::uuid[]) "
        "ORDER BY seen_at, id", ([j.id for j in jobs],))
    by_job: dict[str, list[tuple[str, str]]] = {}
    for job_id, source, url in cur.fetchall():
        by_job.setdefault(job_id, []).append((source, url))
    for job in jobs:
        job.source, job.also_seen_on = primary_source(job.canonical_url, by_job.get(job.id, []))
    return jobs


def load_jobs(conn, where: str, params: tuple = (), order: str = "j.tier, j.score DESC, j.first_seen_at"
              ) -> list[DeliveryJob]:
    with conn.cursor() as cur:
        cur.execute(f"{SELECT_DELIVERY_JOB} WHERE {where} ORDER BY {order}", params)
        jobs = [DeliveryJob(*row) for row in cur.fetchall()]
        return attach_sources(cur, jobs)


def to_scored_offer(job: DeliveryJob) -> ScoredOffer:
    """The note number stands in for the old per-run offer id: it is what ends up
    in note file names, so the naming scheme is unchanged."""
    return ScoredOffer(
        id=job.note_no, title=job.title, company=job.company, location=job.location_raw,
        link=job.canonical_url, description=job.description,
        description_status=job.description_status if job.description_status in
        ("ok", "partial", "failed", "rate_limited") else "ok",
        work_mode=job.work_mode, remote_verdict=job.remote_verdict or "not_checked",
        remote_reason=job.remote_reason, score=job.score, comment=job.comment, summary=job.summary)
