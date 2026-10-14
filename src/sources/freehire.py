"""freehire.me collector (spec section 4.3, source 1).

freehire.me is an open-source aggregator with a documented public API (no key;
600 requests/minute on the ordinary reads, 300 on the full-description endpoint).
We use `GET /agent/jobs/search` only: it returns the FULL description in the same
response (description_format=text), so no per-job request is ever needed. Results
are newest first, so the seen-diff stop is valid here (unlike LinkedIn's
relevance order), and one pull per day matches freehire's own daily rollup.

Provenance: a hit's `source` is where freehire got it. Some come through
aggregators (whatjobs, jobgether) with a tracking URL instead of the employer's
page; those are excluded server side (`exclude_sources`) and never become the
canonical link. `sightings.source_detail` keeps the origin for the provenance report.
"""
import html
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

from src.jobstore.sightings import CardInput
from src.linkedin.catalog import SliceSpec
from src.sources.base import (
    JsonClient, SourceConfig, SourceError, SourceThrottled, SourceWalk, record_cards,
)

SOURCE = "freehire"
SEARCH_PATH = "/agent/jobs/search"
PAGE_URL = "https://freehire.me/jobs/{slug}"
MAX_OFFSET_PLUS_LIMIT = 10000

# Tier 1 Italy, tier 2 Switzerland and San Marino, tier 3 the EU plus Norway, Iceland
# and Liechtenstein (Italy comes back too and is put in tier 1 by its country), tier 4 the UK.
SLICE_AREAS = {
    1: "countries=it",
    2: "countries=ch&countries=sm",
    3: "regions=eu&countries=no&countries=is&countries=li",
    4: "countries=gb",
}

_ISO2 = {
    "it": "italy", "ch": "switzerland", "sm": "san marino", "gb": "united kingdom",
    "at": "austria", "be": "belgium", "bg": "bulgaria", "hr": "croatia", "cy": "cyprus",
    "cz": "czechia", "dk": "denmark", "ee": "estonia", "fi": "finland", "fr": "france",
    "de": "germany", "gr": "greece", "hu": "hungary", "ie": "ireland", "lv": "latvia",
    "lt": "lithuania", "lu": "luxembourg", "mt": "malta", "nl": "netherlands", "pl": "poland",
    "pt": "portugal", "ro": "romania", "sk": "slovakia", "si": "slovenia", "es": "spain",
    "se": "sweden", "no": "norway", "is": "iceland", "li": "liechtenstein",
    "us": "united states", "ca": "canada", "in": "india", "au": "australia", "il": "israel",
    "tr": "turkey", "ua": "ukraine", "rs": "serbia", "ru": "russia",
}
_TAGS = re.compile(r"<[^>]+>")


def freehire_country(codes: list[str]) -> str | None:
    """One canonical country, or None when the hit spans several or an unknown one.
    A hit that lists Italy among several countries is an Italy job for tiering."""
    if "it" in codes:
        return "italy"
    names = {_ISO2[c] for c in codes if c in _ISO2}
    return names.pop() if len(codes) == 1 and len(names) == 1 else None


def clean_text(text: str) -> str:
    """Entities decoded, any stray tags dropped, blank runs collapsed."""
    plain = _TAGS.sub(" ", html.unescape(text or ""))
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{3,}", "\n\n", plain)).strip()


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def item_to_card(item: dict, slice_id: int, tier_hint: int, skip_reality: tuple = ()) -> CardInput | None:
    """A CardInput for one search hit, or None for a hit we do not take."""
    slug, title, company = item.get("public_slug"), item.get("title"), item.get("company")
    if not (slug and title and company) or item.get("closed_at"):
        return None
    if (item.get("reality") or {}).get("class") in skip_reality:
        return None
    codes = [str(c).lower() for c in item.get("countries") or []]
    description = clean_text(item.get("description", ""))
    return CardInput(
        source=SOURCE, source_job_id=slug, source_url=PAGE_URL.format(slug=slug),
        title=title.strip(), company=company.strip(), location=item.get("location") or "N/A",
        posted_at=parse_time(item.get("posted_at") or item.get("created_at")), slice_id=slice_id,
        canonical_url=item.get("url") or None, country=freehire_country(codes),
        remote_hint=item.get("work_mode"), tier_hint=tier_hint, description=description or None,
        description_source=SOURCE if description else None, source_detail=item.get("source"))


def build_slices(cfg: SourceConfig) -> list[SliceSpec]:
    seconds = cfg.interval_hours * 3600
    return [SliceSpec(SOURCE, category, area, tier, seconds)
            for tier, area in SLICE_AREAS.items() for category in cfg.options["categories"]]


def _params(cfg: SourceConfig, row, offset: int, posted_within_days: int) -> list[tuple]:
    options = cfg.options
    params = [("limit", options["page_size"]), ("offset", offset), ("sort", "posted_at"),
              ("order", "desc"), ("description_format", "text"),
              ("posted_within_days", posted_within_days), ("category", row.role)]
    params += parse_qsl(row.area)
    params += [("source_exclude", slug) for slug in options.get("exclude_sources", [])]
    return params


def posted_window_days(cfg: SourceConfig, row, now: datetime) -> int:
    """30 days on the first pull, then the days since the last complete pull plus two."""
    if row.last_walked_at is None:
        return cfg.options["first_pull_days"]
    return max((now - row.last_walked_at) // timedelta(days=1) + 2, 2)


def walk_slice(conn, client: JsonClient, cfg: SourceConfig, row, now: datetime) -> SourceWalk:
    options = cfg.options
    limit = options["page_size"]
    skip = tuple(options.get("skip_reality", []))
    days = posted_window_days(cfg, row, now)
    offset = row.resume_start
    pages = cards_seen = new_jobs = known_streak = 0
    stop = "end_of_results"
    while True:
        if offset + limit > MAX_OFFSET_PLUS_LIMIT:
            stop = "cap_hit"
            break
        try:
            body = client.get_json(SEARCH_PATH, _params(cfg, row, offset, days))
        except SourceThrottled:
            stop = "rate_limited"
            break
        except SourceError:
            stop = "error"
            break
        ignored = (body.get("meta") or {}).get("ignored_params")
        if ignored:
            raise SourceError(f"freehire ignored parameters {ignored}: results would be unfiltered")
        items = body.get("data") or []
        pages += 1
        cards = [c for c in (item_to_card(i, row.id, row.tier, skip) for i in items) if c is not None]
        new, all_known = record_cards(conn, cards, now)
        cards_seen += len(items)
        new_jobs += new
        offset += len(items)
        known_streak = known_streak + 1 if (all_known and len(items) >= limit) else 0
        if len(items) < limit:
            stop = "end_of_results"
            break
        if known_streak >= options["known_pages_to_stop"]:
            stop = "known_page"
            break
    return SourceWalk(pages, cards_seen, new_jobs, stop, offset)
