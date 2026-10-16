"""Fetch a job description from LinkedIn's guest jobPosting fragment.

This replaces the full /jobs/view/{id} page: the fragment is a few KB, carries
the same div.show-more-less-html__markup description, and avoids the HTTP 999
that the full page returns to logged-out clients. A refusal is never turned into
text: the caller keeps the job and tries later.

A 429/999 is one refusal and returns at once as `"throttled"`: the host-level
wait in the pace applies before the next request, and the caller gives the job no
per-job penalty. The 5/15/45 s ladder is for the job-specific refusals (401, 403,
503, 504) and network errors, which end as `"refused"`.
"""
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from src.http_session import retry_after_seconds
from src.linkedin.pace import PaceGateClosed
from src.linkedin.search import HEADERS, RETRY_WAITS, THROTTLE_STATUSES
from src.services.pulse import pulse

DESCRIPTION_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
REQUEST_TIMEOUT = 15
JOB_REFUSED_STATUSES = frozenset({401, 403, 503, 504})
REFUSED_STATUSES = frozenset({401, 403, 429, 503, 504, 999})
GONE_STATUSES = frozenset({404, 410})


@dataclass(frozen=True)
class FragmentResult:
    status: str  # ok | throttled | refused | gone | failed | deferred
    text: str = ""
    posted_text: str = ""


def extract_description(html: str) -> str:
    """The description text, extracted exactly as the old scraper did
    (get_text(strip=True)), so stored verdicts stay comparable with new ones."""
    element = BeautifulSoup(html, "html.parser").find("div", class_="show-more-less-html__markup")
    return element.get_text(strip=True) if element else ""


def extract_posted_text(html: str) -> str:
    """The "posted N ago" text the description fragment already carries, for the
    schedule study's hourly pattern. No extra request; empty when LinkedIn changed
    the markup."""
    element = BeautifulSoup(html, "html.parser").find("span", class_="posted-time-ago__text")
    return element.get_text(strip=True) if element else ""


def fetch_description(pace, job_id: str, *, overdue: bool = False,
                      get=requests.get, sleep=time.sleep) -> FragmentResult:
    url = DESCRIPTION_URL.format(job_id=job_id)
    last_problem = "no attempt"
    for attempt in range(len(RETRY_WAITS) + 1):
        pulse({"description": job_id, "attempt": attempt + 1})
        try:
            with pace.slot("description", overdue=overdue) as slot:
                try:
                    response = get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
                except requests.RequestException as exc:
                    slot.neutral()
                    last_problem = f"network error: {exc}"
                    response = None
                if response is not None:
                    status = response.status_code
                    if status == 200:
                        slot.clean(status)
                        text = extract_description(response.text)
                        posted = extract_posted_text(response.text)
                        return FragmentResult("ok", text, posted) if text else FragmentResult("failed")
                    if status in GONE_STATUSES:
                        slot.neutral(status)
                        return FragmentResult("gone")
                    if status in THROTTLE_STATUSES:
                        retry_after = retry_after_seconds(
                            response.headers.get("Retry-After"), datetime.now(timezone.utc))
                        slot.throttled(status, retry_after=retry_after)
                        return FragmentResult("throttled")
                    if status in JOB_REFUSED_STATUSES:
                        slot.neutral(status)
                        last_problem = f"HTTP {status}"
                    else:
                        slot.neutral(status)
                        return FragmentResult("failed")
        except PaceGateClosed:
            # The schedule gate (quiet hours, the daily cap) closed between requests:
            # hand the job back untouched, the caller retries next tick.
            return FragmentResult("deferred")
        if attempt < len(RETRY_WAITS):
            sleep(RETRY_WAITS[attempt])
    return FragmentResult("refused")
