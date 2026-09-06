import random
import time
import requests
from bs4 import BeautifulSoup
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

_PAGE_SIZE = 25
# Deliberately conservative. The captain asked to revisit this against real
# observed page counts after the first production run of the reshaped tiers -
# raising it multiplies requests per query and therefore rate-limit exposure.
_MAX_PAGES_PER_QUERY = 8


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
) -> list[JobOffer]:
    work_modes = work_modes or []
    modes = work_modes if work_modes else [None]
    locations = countries if countries else [location]

    all_offers: list[JobOffer] = []
    seen_links: set[str] = set()
    offer_id = 0

    for loc in locations:
        for role in roles:
            for mode in modes:
                offers = _fetch_for_query(
                    role, loc, time_range, mode,
                    start_id=offer_id,
                    allowed_countries=allowed_countries,
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
            break

        if response.status_code in (429, 503, 504):
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
) -> list[JobOffer]:
    offers: list[JobOffer] = []
    seen_links: set[str] = set()
    next_id = start_id
    last_page_was_full = False

    for page in range(_MAX_PAGES_PER_QUERY):
        if page:
            # A page whose cards are all out of scope fetches no descriptions,
            # so without this the loop can fire every search request back to
            # back. Same pacing the card loop already applies.
            time.sleep(random.uniform(1.5, 3.0))
        try:
            response = _fetch_search_page(role, location, time_range, work_mode, page * _PAGE_SIZE)
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
            break
        cards = _parse_cards(response.text)
        if not cards:
            break

        new_cards = [c for c in cards if c["link"] not in seen_links]
        if not new_cards:
            # LinkedIn repeats the last page instead of returning an empty one
            # once a query is exhausted, so a page with nothing new ends it.
            break
        last_page_was_full = len(cards) == _PAGE_SIZE

        for card in new_cards:
            seen_links.add(card["link"])
            # Scope is decided BEFORE the description fetch: that fetch is an
            # extra request plus a multi-second sleep, and a discarded card
            # must never cost one.
            if not is_in_scope(card["location"], allowed_countries):
                continue
            description, description_status = _fetch_description(
                card["link"], card["title"], card["company"]
            )
            time.sleep(random.uniform(1.5, 3.0))
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
        # An under-full final page exhausted the result set on its own, so only
        # a full last page leaves it ambiguous whether the cap truncated this
        # query. The cap stays where it is until production shows real page
        # depth, and that observation needs this line to be free of false
        # positives.
        if last_page_was_full:
            print(f"[scraper] Hit the page cap ({_MAX_PAGES_PER_QUERY} pages) for "
                  f"{role}/{location}/{work_mode} - there may be more results beyond this.")

    return offers


def _fetch_description(url: str, title: str, company: str) -> tuple[str, str]:
    fallback = f"{title} at {company}"

    for attempt in range(_DESC_MAX_RETRIES):
        try:
            response = requests.get(url, headers=HEADERS, timeout=15)
        except requests.RequestException:
            if attempt == _DESC_MAX_RETRIES - 1:
                return "", "failed"
            wait = _wait_with_jitter(_DESC_BASE_WAIT * (2 ** min(attempt, 3)), _DESC_WAIT_CAP)
            time.sleep(wait)
            continue

        if response.status_code == 200:
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
            if attempt == _DESC_MAX_RETRIES - 1:
                return "", "failed"
            wait = _wait_with_jitter(_DESC_BASE_WAIT * (2 ** min(attempt, 3)), _DESC_WAIT_CAP)
            print(f"[scraper] description HTTP {response.status_code}, retrying in {wait:.0f}s...")
            time.sleep(wait)
        else:
            # non-retriable (403, 404, etc.) - use title+company fallback
            return fallback, "partial"

    return "", "failed"
