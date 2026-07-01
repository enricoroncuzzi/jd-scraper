import random
import time
import requests
from bs4 import BeautifulSoup
from src.models import JobOffer

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

_WORK_MODE_MAP = {"remote": "2", "hybrid": "3"}

# Retry config — no hard time constraint, so be patient with LinkedIn rate limits.
# Schedule: 60s → 120 → 240 → 480 → 900 → 900 → ... (±25% jitter each step)
_SEARCH_MAX_RETRIES = 20
_SEARCH_BASE_WAIT = 60   # seconds
_SEARCH_WAIT_CAP = 900   # 15 min max per wait

_DESC_MAX_RETRIES = 8
_DESC_BASE_WAIT = 30
_DESC_WAIT_CAP = 300


def _wait_with_jitter(base_seconds: float, cap: float) -> float:
    jittered = base_seconds * random.uniform(0.75, 1.25)
    return min(jittered, cap)


def fetch_offers(
    roles: list[str],
    location: str,
    time_range: str,
    work_modes: list[str] = None,
    countries: list[str] = None,
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
                offers = _fetch_for_query(role, loc, time_range, mode, start_id=offer_id)
                for offer in offers:
                    if offer.link not in seen_links:
                        seen_links.add(offer.link)
                        all_offers.append(offer)
                        offer_id += 1

    return all_offers


def _fetch_for_query(
    role: str,
    location: str,
    time_range: str,
    work_mode: str | None,
    start_id: int = 0,
) -> list[JobOffer]:
    params = {"keywords": role, "location": location, "f_TPR": time_range, "start": 0}
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
        else:
            raise RuntimeError(f"LinkedIn search returned {response.status_code}")

    soup = BeautifulSoup(response.text, "html.parser")
    cards = soup.find_all("li")

    offers: list[JobOffer] = []
    for i, card in enumerate(cards):
        title_el = card.find("h3", class_="base-search-card__title")
        company_el = card.find("h4", class_="base-search-card__subtitle")
        location_el = card.find("span", class_="job-search-card__location")
        link_el = card.find("a", class_="base-card__full-link")

        if not title_el or not link_el:
            continue

        title = title_el.get_text(strip=True)
        company = company_el.get_text(strip=True) if company_el else "N/A"
        loc = location_el.get_text(strip=True) if location_el else "N/A"
        link = link_el["href"].split("?")[0]

        description, description_status = _fetch_description(link, title, company)
        time.sleep(random.uniform(1.5, 3.0))

        offers.append(JobOffer(
            id=start_id + i,
            title=title,
            company=company,
            location=loc,
            link=link,
            description=description,
            description_status=description_status,
            work_mode=work_mode or "",
        ))

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
            # non-retriable (403, 404, etc.) — use title+company fallback
            return fallback, "partial"

    return "", "failed"
