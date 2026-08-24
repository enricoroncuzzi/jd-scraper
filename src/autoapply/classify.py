import re
import urllib.parse
import requests
from bs4 import BeautifulSoup

CHANNEL_LINKEDIN_EASY_APPLY = "linkedin_easy_apply"
CHANNEL_EXTERNAL_ATS = "external_ats"
CHANNEL_EMAIL_APPLY = "email_apply"
CHANNEL_UNKNOWN = "unknown"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

_REQUEST_TIMEOUT = 15

_KNOWN_ATS_DOMAINS = (
    "greenhouse.io", "lever.co", "myworkdayjobs.com", "workday.com",
    "ashbyhq.com", "smartrecruiters.com", "icims.com", "bamboohr.com",
    "jobvite.com", "successfactors.com", "taleo.net", "recruitee.com",
    "personio.de", "personio.com", "breezy.hr", "workable.com",
    "jazzhr.com", "teamtailor.com", "join.com",
)

_OFFSITE_APPLY_ATTR = "data-tracking-control-name"
_OFFSITE_APPLY_VALUE = "public_jobs_apply-link-offsite"


def _host(url: str) -> str:
    return urllib.parse.urlparse(url).netloc.lower()


def _is_linkedin_host(host: str) -> bool:
    return host.endswith("linkedin.com")


def _is_known_ats_host(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in _KNOWN_ATS_DOMAINS)


def _mailto_href(soup: BeautifulSoup) -> str | None:
    a = soup.find("a", href=re.compile(r"^mailto:", re.IGNORECASE))
    return a["href"] if a else None


def _offsite_apply_href(soup: BeautifulSoup) -> str | None:
    a = soup.find("a", attrs={_OFFSITE_APPLY_ATTR: _OFFSITE_APPLY_VALUE})
    if a and a.get("href"):
        return a["href"]
    # fallback: any anchor whose visible text mentions "apply" and points off linkedin.com
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True).lower()
        if "apply" in text and "linkedin.com" not in _host(a["href"]) and a["href"].startswith("http"):
            return a["href"]
    return None


def _has_easy_apply_marker(soup: BeautifulSoup) -> bool:
    if soup.find(string=re.compile(r"easy apply", re.IGNORECASE)):
        return True
    return soup.find(attrs={"class": re.compile(r"easy-apply", re.IGNORECASE)}) is not None


def classify_channel(link: str, timeout: float = _REQUEST_TIMEOUT) -> str:
    """Resolve `link` (a captured LinkedIn job-posting URL), follow redirects, and read
    the resulting page to bucket the application channel. Read-only HTTP GET requests
    only — never logs in, never drives a UI, never submits anything."""
    try:
        resp = requests.get(link, headers=HEADERS, timeout=timeout, allow_redirects=True)
    except requests.RequestException:
        return CHANNEL_UNKNOWN

    if resp.status_code != 200:
        return CHANNEL_UNKNOWN

    host = _host(resp.url)

    if not _is_linkedin_host(host):
        # the captured link redirected straight off LinkedIn already
        if _is_known_ats_host(host):
            return CHANNEL_EXTERNAL_ATS
        return CHANNEL_EXTERNAL_ATS if host else CHANNEL_UNKNOWN

    soup = BeautifulSoup(resp.text, "html.parser")

    if _has_easy_apply_marker(soup):
        return CHANNEL_LINKEDIN_EASY_APPLY

    offsite_href = _offsite_apply_href(soup)
    if offsite_href:
        return CHANNEL_EXTERNAL_ATS

    if _mailto_href(soup):
        return CHANNEL_EMAIL_APPLY

    return CHANNEL_UNKNOWN
