import random
import time
import requests
from bs4 import BeautifulSoup
from src import telemetry
from src.models import JobOffer
from src.tier_scope import is_in_scope

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

_WORK_MODE_MAP = {"remote": "2", "hybrid": "3"}

# Retry config - no hard time constraint, so be patient with LinkedIn rate limits.
# Schedule: 60s -> 120 -> 240 -> 480 -> 900 -> 900 -> ... (+/-25% jitter each step)
_SEARCH_MAX_RETRIES = 20
_SEARCH_BASE_WAIT = 60   # seconds
_SEARCH_WAIT_CAP = 900   # 15 min max per wait

_DESC_MAX_RETRIES = 8
_DESC_BASE_WAIT = 30
_DESC_WAIT_CAP = 300

# Gap between search pages and description requests. A 429 raises it, and a
# later request that was not rate-limited puts it back. The cap stops a long
# 429 streak from turning the gap into an unbounded sleep.
_PACE_LOW = 1.5
_PACE_HIGH = 3.0
_PACE_DELAY_CAP = 30.0
_PACE_GROWTH = 2.0


class _RequestPace:
    def __init__(self):
        self.factor = 1.0

    def reset(self) -> None:
        self.factor = 1.0

    def slow(self) -> None:
        self.factor = min(self.factor * _PACE_GROWTH, _PACE_DELAY_CAP / _PACE_LOW)

    def relax(self) -> None:
        # One clean request steps back toward the normal gap. A run of 429s
        # is not forgotten by the next response that happened to succeed.
        self.factor = max(1.0, self.factor / _PACE_GROWTH)

    def pause(self) -> None:
        low = min(_PACE_LOW * self.factor, _PACE_DELAY_CAP)
        high = min(_PACE_HIGH * self.factor, _PACE_DELAY_CAP)
        if high < low:
            high = low
        time.sleep(random.uniform(low, high))


_pace = _RequestPace()


def _note_response(status_code: int, saw_429: bool) -> bool:
    """Raise the inter-request gap on HTTP 429. A completed request that never
    saw a 429 puts the gap back to normal before the caller sleeps. 503/504
    stay on the retry ladder and do not count as a 429."""
    if status_code == 429:
        _pace.slow()
        return True
    if not saw_429 and status_code not in (503, 504):
        _pace.relax()
    return saw_429

# Deliberately conservative default, used only as the safety fallback below.
# The captain has since reviewed real observed page counts (20 days of
# production logs, see data/scraper-coverage-check/report.md in the firstmate
# home) and set per-tier caps in each tier's config - see
# SearchConfig.max_pages_per_query in src/config.py and each config_tier*.json.
# Since pagination steps by the cards actually returned (see _fetch_for_query),
# this cap is a card budget, not a page-size-dependent one: 8 pages of the
# endpoint's current 10 cards is ~80 offers per query.
_MAX_PAGES_PER_QUERY = 8


def resolve_max_pages_per_query(value: int | None) -> int:
    """Translate a tier config's max_pages_per_query into the cap
    _fetch_for_query() paginates against.

    Returns the deliberately conservative _MAX_PAGES_PER_QUERY default when
    `value` is absent (None) or not a positive int, so a malformed or older
    config can never turn into unbounded pagination.
    """
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return _MAX_PAGES_PER_QUERY
    return value


class _EndOfResults(RuntimeError):
    """HTTP 400: a start offset past the end, which past page 0 means no more results."""


def _wait_with_jitter(base_seconds: float, cap: float) -> float:
    jittered = base_seconds * random.uniform(0.75, 1.25)
    return min(jittered, cap)


def fetch_offers(
    roles: list[str],
    location: str,
    time_range: str,
    work_modes: list[str] = None,
    countries: list[str] = None,
    allowed_countries: frozenset[str] | None = None,
    max_pages_per_query: int = _MAX_PAGES_PER_QUERY,
) -> list[JobOffer]:
    _pace.reset()
    work_modes = work_modes or []
    modes = work_modes if work_modes else [None]
    locations = countries if countries else [location]

    all_offers: list[JobOffer] = []
    seen_links: set[str] = set()
    # Run-scoped description cache, shared by every query of this call and keyed
    # by the same normalized link the merge below dedups on. The merge keeps the
    # first query's offer per link, so a later query's copy of that offer is
    # discarded anyway; the cache just stops it costing a description request
    # (plus its pacing sleep) first.
    fetched_descriptions: dict[str, tuple[str, str]] = {}
    offer_id = 0

    for loc in locations:
        for role in roles:
            for mode in modes:
                offers = _fetch_for_query(
                    role, loc, time_range, mode,
                    start_id=offer_id,
                    allowed_countries=allowed_countries,
                    max_pages_per_query=max_pages_per_query,
                    fetched_descriptions=fetched_descriptions,
                )
                for offer in offers:
                    if offer.link not in seen_links:
                        seen_links.add(offer.link)
                        all_offers.append(offer)
                offer_id += len(offers)

    return all_offers


def _fetch_search_page(role: str, location: str, time_range: str, work_mode: str | None, start: int):
    params = {"keywords": role, "location": location, "f_TPR": time_range, "start": start}
    if work_mode and work_mode in _WORK_MODE_MAP:
        params["f_WT"] = _WORK_MODE_MAP[work_mode]

    response = None
    saw_429 = False
    for attempt in range(_SEARCH_MAX_RETRIES):
        try:
            response = requests.get(SEARCH_URL, params=params, headers=HEADERS, timeout=20)
        except requests.RequestException as exc:
            if attempt == _SEARCH_MAX_RETRIES - 1:
                raise RuntimeError(f"LinkedIn search network error after {_SEARCH_MAX_RETRIES} retries: {exc}") from exc
            wait = _wait_with_jitter(_SEARCH_BASE_WAIT * (2 ** min(attempt, 4)), _SEARCH_WAIT_CAP)
            print(f"[scraper] Network error ({exc}), retrying in {wait:.0f}s (attempt {attempt + 1}/{_SEARCH_MAX_RETRIES})...")
            time.sleep(wait)
            continue

        if response.status_code == 200:
            _note_response(200, saw_429)
            break

        if response.status_code in (429, 503, 504):
            if response.status_code == 429:
                telemetry.count("search_rate_limits")
            saw_429 = _note_response(response.status_code, saw_429)
            if attempt == _SEARCH_MAX_RETRIES - 1:
                raise RuntimeError(f"LinkedIn search returned {response.status_code} after {_SEARCH_MAX_RETRIES} retries")
            wait = _wait_with_jitter(_SEARCH_BASE_WAIT * (2 ** min(attempt, 4)), _SEARCH_WAIT_CAP)
            print(f"[scraper] HTTP {response.status_code}, retrying in {wait:.0f}s (attempt {attempt + 1}/{_SEARCH_MAX_RETRIES})...")
            time.sleep(wait)
        elif response.status_code == 400:
            raise _EndOfResults(f"LinkedIn search returned {response.status_code}")
        else:
            raise RuntimeError(f"LinkedIn search returned {response.status_code}")

    return response


def _parse_cards(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    cards = []
    for card in soup.find_all("li"):
        title_el = card.find("h3", class_="base-search-card__title")
        company_el = card.find("h4", class_="base-search-card__subtitle")
        location_el = card.find("span", class_="job-search-card__location")
        link_el = card.find("a", class_="base-card__full-link")
        if not title_el or not link_el:
            continue
        cards.append({
            "title": title_el.get_text(strip=True),
            "company": company_el.get_text(strip=True) if company_el else "N/A",
            "location": location_el.get_text(strip=True) if location_el else "N/A",
            "link": link_el["href"].split("?")[0],
        })
    return cards


def _fetch_for_query(
    role: str,
    location: str,
    time_range: str,
    work_mode: str | None,
    start_id: int = 0,
    allowed_countries: frozenset[str] | None = None,
    max_pages_per_query: int = _MAX_PAGES_PER_QUERY,
    fetched_descriptions: dict[str, tuple[str, str]] | None = None,
) -> list[JobOffer]:
    """Paginate one search and return its in-scope offers.

    `fetched_descriptions` maps link -> (description, status) for every
    description already fetched earlier in the same fetch_offers() run. A card
    whose link is in it still becomes a JobOffer here (so ids and this query's
    offer count are exactly what they were before the cache existed), but its
    description is reused instead of requested. That is what the telemetry
    record's two counts mean: `offers_kept` is every offer this query returned,
    `cross_query_duplicates` is the subset of those that reused an earlier
    query's fetch, so the description requests this query actually made are
    `offers_kept - cross_query_duplicates`.

    This is separate from the per-query `seen_links` below, which only drives
    the duplicate-page stop and must never see other queries' links.
    """
    if fetched_descriptions is None:
        fetched_descriptions = {}
    offers: list[JobOffer] = []
    seen_links: set[str] = set()
    cross_query_duplicates = 0
    next_id = start_id
    # The endpoint's page size is not ours to assume: it returned 25 cards per
    # request until 2026-09 and 10 now, and a constant stride larger than the
    # real page size silently skipped most of every window (positions 10-24,
    # 35-49, ...). `start` is an absolute offset the endpoint honours, so it
    # advances by the number of cards each response actually consumed.
    next_start = 0
    widest_page = 0
    last_page_was_full = False
    pages_walked = 0
    stop_reason = "error"
    cards_seen = 0

    try:
        for page in range(max_pages_per_query):
            pages_walked = page + 1
            if page:
                # A page whose cards are all out of scope fetches no descriptions,
                # so without this the loop can fire every search request back to
                # back. Same pacing the card loop already applies.
                _pace.pause()
            try:
                response = _fetch_search_page(role, location, time_range, work_mode, next_start)
            except _EndOfResults:
                if page == 0:
                    raise
                # LinkedIn answers a start offset past the end of the result set
                # with HTTP 400 instead of an empty page, and only that status
                # raises _EndOfResults. Treat it as end-of-results on any page
                # after the first, rather than discarding every offer this query
                # already fetched and forcing a full tier restart from page 0.
                # Every other non-retriable status (403, 404, ...) and retry-ladder
                # exhaustion raise a plain RuntimeError instead and keep
                # propagating, so a block or a real outage still reaches the tier
                # retry rather than silently truncating the query.
                print(f"[scraper] Hard error on page {page} ({role}/{location}/{work_mode}) "
                      f"- ending pagination here, keeping {len(offers)} offers already fetched.")
                stop_reason = "end_of_results"
                break
            cards = _parse_cards(response.text)
            cards_seen += len(cards)
            if not cards:
                stop_reason = "empty_page"
                break

            new_cards = [c for c in cards if c["link"] not in seen_links]
            if not new_cards:
                # LinkedIn repeats the last page instead of returning an empty one
                # once a query is exhausted, so a page with nothing new ends it.
                stop_reason = "duplicate_page"
                break
            # "Full" is measured against the widest page this query has actually
            # seen, i.e. the endpoint's own page size as observed right now, so
            # the cap-hit report below keeps working when that size moves again.
            last_page_was_full = len(cards) >= widest_page
            widest_page = max(widest_page, len(cards))
            next_start += len(cards)

            for card in new_cards:
                seen_links.add(card["link"])
                # Scope is decided BEFORE the description fetch: that fetch is an
                # extra request plus a multi-second sleep, and a discarded card
                # must never cost one.
                if not is_in_scope(card["location"], allowed_countries):
                    continue
                cached = fetched_descriptions.get(card["link"])
                if cached is not None:
                    description, description_status = cached
                    cross_query_duplicates += 1
                else:
                    description, description_status = _fetch_description(
                        card["link"], card["title"], card["company"]
                    )
                    fetched_descriptions[card["link"]] = (description, description_status)
                    _pace.pause()
                offers.append(JobOffer(
                    id=next_id,
                    title=card["title"],
                    company=card["company"],
                    location=card["location"],
                    link=card["link"],
                    description=description,
                    description_status=description_status,
                    work_mode=work_mode or "",
                ))
                next_id += 1
        else:
            stop_reason = "cap_hit" if last_page_was_full else "exhausted_underfull"
            # An under-full final page exhausted the result set on its own, so only
            # a full last page leaves it ambiguous whether the cap truncated this
            # query. The cap stays where it is until production shows real page
            # depth, and that observation needs this line to be free of false
            # positives - hence "full" meaning "as wide as this query's other
            # pages", never "== some hardcoded page size".
            if last_page_was_full:
                print(f"[scraper] Hit the page cap ({max_pages_per_query} pages) for "
                      f"{role}/{location}/{work_mode} - there may be more results beyond this.")
    finally:
        telemetry.add_query(
            role=role, location=location, work_mode=work_mode,
            pages_walked=pages_walked, page_cap=max_pages_per_query,
            cards_seen=cards_seen, offers_kept=len(offers),
            cross_query_duplicates=cross_query_duplicates, stop_reason=stop_reason,
        )

    # One line per query, printed regardless of how pagination stopped, so
    # "did this query hit its cap" is answerable from the cron log alone
    # rather than by inferring it from the (cap-hit-only) warning above.
    reused = (f" ({cross_query_duplicates} reused from an earlier query)"
              if cross_query_duplicates else "")
    print(f"[scraper] Paginated {role}/{location}/{work_mode}: "
          f"{pages_walked}/{max_pages_per_query} pages, {len(offers)} offers kept{reused}.")

    return offers


def refetch_description(offer: JobOffer) -> JobOffer:
    """Fetch one carried-over offer's description again.

    Used when an earlier run stored the offer because LinkedIn rate-limited
    the page. The inter-request gap stays wherever this run's 429s left it.
    """
    description, status = _fetch_description(offer.link, offer.title, offer.company)
    _pace.pause()
    return offer.model_copy(update={"description": description, "description_status": status})


def _fetch_description(url: str, title: str, company: str) -> tuple[str, str]:
    fallback = f"{title} at {company}"
    saw_429 = False

    for attempt in range(_DESC_MAX_RETRIES):
        try:
            response = requests.get(url, headers=HEADERS, timeout=15)
        except requests.RequestException:
            if attempt == _DESC_MAX_RETRIES - 1:
                # A 429 earlier in this fetch means the page was throttled,
                # even if the last attempt died on the network instead.
                return "", "rate_limited" if saw_429 else "failed"
            wait = _wait_with_jitter(_DESC_BASE_WAIT * (2 ** min(attempt, 3)), _DESC_WAIT_CAP)
            time.sleep(wait)
            continue

        if response.status_code == 200:
            _note_response(200, saw_429)
            soup = BeautifulSoup(response.text, "html.parser")

            # Step 1: main LinkedIn div
            desc_el = soup.find("div", class_="show-more-less-html__markup")
            if desc_el:
                text = desc_el.get_text(strip=True)
                if text:
                    return text, "ok"

            # Step 2: meta description tag
            meta = soup.find("meta", attrs={"name": "description"})
            if meta and meta.get("content", "").strip():
                return meta["content"].strip(), "partial"

            # Step 3: first substantial paragraph or article
            for tag in soup.find_all(["p", "article"]):
                text = tag.get_text(strip=True)
                if len(text) > 50:
                    return text, "partial"

            # Step 4: title + company as last resort
            return fallback, "partial"

        if response.status_code in (429, 503, 504):
            if response.status_code == 429:
                telemetry.count("description_rate_limits")
            saw_429 = _note_response(response.status_code, saw_429)
            if attempt == _DESC_MAX_RETRIES - 1:
                # Not the title/company fallback: that text would be verified
                # and scored as if it were the job. The caller defers the offer.
                return "", "rate_limited"
            wait = _wait_with_jitter(_DESC_BASE_WAIT * (2 ** min(attempt, 3)), _DESC_WAIT_CAP)
            print(f"[scraper] description HTTP {response.status_code}, retrying in {wait:.0f}s...")
            time.sleep(wait)
        else:
            # non-retriable (403, 404, etc.) - use title+company fallback
            _note_response(response.status_code, saw_429)
            return fallback, "partial"

    return "", "failed"
