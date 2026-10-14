"""Himalayas collector (spec section 4.3, source 2).

Himalayas (himalayas.app) is a remote-only board with a free public JSON API. Its
data is cached for 24 hours, so one pull per day is all there is to see. Remote is
true by construction; what matters is who may apply:

- `locationRestrictions` empty means worldwide, which includes Italy;
- otherwise the job is usable only if Italy is on the list;
- `timezoneRestrictions` (UTC offsets) must be empty or include Italy's (+1/+2).

The feed carries the full description as HTML in the same response. Attribution:
Himalayas asks for a visible link back and a mention that the data comes from it;
the note's `source` field and its Himalayas link do that (src/delivery/render.py adds
a source line for Himalayas jobs).
"""
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from src.jobstore.sightings import CardInput
from src.linkedin.catalog import SliceSpec
from src.sources.base import (
    JsonClient, SourceConfig, SourceError, SourceThrottled, SourceWalk, record_cards,
)

SOURCE = "himalayas"
SEARCH_PATH = "/search"
PAGE_SIZE = 20
ITALY_UTC_OFFSETS = (1, 2)
WORLDWIDE_TIER = 3  # Europe-wide remote, workable from Italy: the same place LinkedIn's "Europe"/"Remote" locations land


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    country: str | None = None
    tier_hint: int | None = None
    remote_hint: str | None = None
    reason: str = ""


def parse_time(value) -> datetime | None:
    """pubDate and expiryDate are documented as ISO strings but arrive as epoch seconds."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or str(value).replace(".", "", 1).isdigit():
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def html_to_text(markup: str) -> str:
    text = BeautifulSoup(markup or "", "html.parser").get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def italy_eligibility(job: dict) -> Eligibility:
    restrictions = job.get("locationRestrictions") or []
    offsets = job.get("timezoneRestrictions") or []
    if not isinstance(restrictions, list) or not isinstance(offsets, list):
        return Eligibility(False, reason="malformed")
    if offsets and not any(offset in offsets for offset in ITALY_UTC_OFFSETS):
        return Eligibility(False, reason="timezone")
    if not restrictions:
        return Eligibility(True, None, WORLDWIDE_TIER, "remote_worldwide")
    if "Italy" in restrictions:
        return Eligibility(True, "italy", 1, "remote")
    return Eligibility(False, reason="location")


def item_to_card(job: dict, slice_id: int, now: datetime) -> CardInput | None:
    """A card for one feed item, or None. Total: a malformed item never raises, it is skipped."""
    if not isinstance(job, dict):
        return None
    guid = job.get("guid") or job.get("applicationLink")
    title = job.get("title")
    company = job.get("companyName")
    if not (isinstance(guid, str) and guid.strip()
            and isinstance(title, str) and title.strip()
            and isinstance(company, str) and company.strip()):
        return None
    expires = parse_time(job.get("expiryDate"))
    if expires is not None and expires < now:
        return None
    eligibility = italy_eligibility(job)
    if not eligibility.eligible:
        return None
    restrictions = job.get("locationRestrictions") or []
    link = job.get("applicationLink")
    if not (isinstance(link, str) and link.strip()):
        link = guid
    description = html_to_text(job.get("description", "")) or (job.get("excerpt") or "").strip()
    return CardInput(
        source=SOURCE, source_job_id=guid, source_url=link,
        title=title.strip(), company=company.strip(),
        location=", ".join(restrictions) if restrictions else "Worldwide",
        posted_at=parse_time(job.get("pubDate")), slice_id=slice_id, country=eligibility.country,
        remote_hint=eligibility.remote_hint, tier_hint=eligibility.tier_hint,
        description=description or None, description_source=SOURCE if description else None,
        source_detail=job.get("employmentType"))


def build_slices(cfg: SourceConfig) -> list[SliceSpec]:
    seconds = cfg.interval_hours * 3600
    area = f"country={cfg.options['country']}"
    return [SliceSpec(SOURCE, role, area, WORLDWIDE_TIER, seconds) for role in cfg.options["roles"]]


def walk_slice(conn, client: JsonClient, cfg: SourceConfig, row, now: datetime) -> SourceWalk:
    options = cfg.options
    page = row.resume_start // PAGE_SIZE + 1
    pages = cards_seen = new_jobs = known_streak = 0
    stop = "end_of_results"
    for _ in range(options["max_pages"]):
        try:
            body = client.get_json(SEARCH_PATH, {"q": row.role, "country": options["country"],
                                                  "sort": "recent", "page": page})
        except SourceThrottled:
            stop = "rate_limited"
            break
        except SourceError:
            stop = "error"
            break
        if not isinstance(body, dict):
            stop = "error"
            break
        jobs = body.get("jobs") or []
        if not isinstance(jobs, list):
            stop = "error"
            break
        pages += 1
        cards = [c for c in (item_to_card(j, row.id, now) for j in jobs if isinstance(j, dict))
                 if c is not None]
        new, all_known = record_cards(conn, cards, now)
        cards_seen += len(jobs)
        new_jobs += new
        page += 1
        known_streak = known_streak + 1 if (all_known and len(jobs) >= PAGE_SIZE) else 0
        if len(jobs) < PAGE_SIZE:
            break
        if known_streak >= options["known_pages_to_stop"]:
            stop = "known_page"
            break
    else:
        stop = "cap_hit"
    return SourceWalk(pages, cards_seen, new_jobs, stop, (page - 1) * PAGE_SIZE)
