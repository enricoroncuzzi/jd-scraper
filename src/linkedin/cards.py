"""Parse LinkedIn guest search result pages into cards."""
from dataclasses import dataclass
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from src.jobstore.identity import linkedin_job_id


@dataclass(frozen=True)
class Card:
    job_id: str
    url: str
    title: str
    company: str
    location: str
    posted_at: datetime | None


def _posted_at(card) -> datetime | None:
    """The card's <time datetime="YYYY-MM-DD"> if present. Absent or malformed gives None."""
    element = card.find("time")
    value = element.get("datetime", "").strip() if element else ""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _entity_job_id(card) -> str | None:
    """The numeric id in data-entity-urn="urn:li:jobPosting:123", when the card has one."""
    holder = card.find(attrs={"data-entity-urn": True})
    urn = holder.get("data-entity-urn", "") if holder else ""
    tail = urn.rsplit(":", 1)[-1]
    return tail if tail.isdigit() else None


def _clean_text(element) -> str:
    """Visible text with NUL removed: Postgres TEXT refuses a literal NUL, and one
    poisoned card would otherwise wedge the whole collector on that slice."""
    return element.get_text(strip=True).replace("\x00", "") if element else ""


def parse_cards(html: str) -> list[Card]:
    """Cards with a title, a link and a job id. Same selectors the old scraper used."""
    soup = BeautifulSoup(html, "html.parser")
    cards = []
    for item in soup.find_all("li"):
        title = item.find("h3", class_="base-search-card__title")
        link = item.find("a", class_="base-card__full-link")
        if not title or not link or not link.get("href"):
            continue
        url = link["href"].split("?")[0]
        job_id = linkedin_job_id(url) or _entity_job_id(item)
        if not job_id:
            continue
        company = item.find("h4", class_="base-search-card__subtitle")
        location = item.find("span", class_="job-search-card__location")
        cards.append(Card(
            job_id=job_id,
            url=url,
            title=_clean_text(title),
            company=_clean_text(company) or "N/A",
            location=_clean_text(location) or "N/A",
            posted_at=_posted_at(item),
        ))
    return cards
