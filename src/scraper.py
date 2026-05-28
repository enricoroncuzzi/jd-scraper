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


def fetch_offers(role: str, location: str, time_range: str) -> list[JobOffer]:
    params = {"keywords": role, "location": location, "f_TPR": time_range, "start": 0}
    response = requests.get(SEARCH_URL, params=params, headers=HEADERS, timeout=15)
    if response.status_code != 200:
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

        description = _fetch_description(link)
        time.sleep(1)

        offers.append(JobOffer(
            id=i,
            title=title,
            company=company,
            location=loc,
            link=link,
            description=description,
        ))

    return offers


def _fetch_description(url: str) -> str:
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        if response.status_code != 200:
            return ""
        soup = BeautifulSoup(response.text, "html.parser")
        desc_el = soup.find("div", class_="show-more-less-html__markup")
        return desc_el.get_text(strip=True) if desc_el else ""
    except Exception:
        return ""
