"""Fetch a job description from LinkedIn's guest jobPosting fragment.

This replaces the full /jobs/view/{id} page: the fragment is a few KB, carries
the same div.show-more-less-html__markup description, and avoids the HTTP 999
that the full page returns to logged-out clients. A refusal (429, 999, 503, 504,
401, 403) is never turned into text: the caller keeps the job and tries later.
"""
import time
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

from src.linkedin.search import HEADERS, RETRY_WAITS
from src.services.pulse import pulse

DESCRIPTION_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
REQUEST_TIMEOUT = 15
THROTTLE_STATUSES = frozenset({429, 999})
REFUSED_STATUSES = frozenset({401, 403, 429, 503, 504, 999})
GONE_STATUSES = frozenset({404, 410})


@dataclass(frozen=True)
class FragmentResult:
    status: str  # ok | refused | gone | failed
    text: str = ""


def extract_description(html: str) -> str:
    """The description text, extracted exactly as the old scraper did
    (get_text(strip=True)), so stored verdicts stay comparable with new ones."""
    element = BeautifulSoup(html, "html.parser").find("div", class_="show-more-less-html__markup")
    return element.get_text(strip=True) if element else ""


def fetch_description(pace, job_id: str, *, get=requests.get, sleep=time.sleep) -> FragmentResult:
    url = DESCRIPTION_URL.format(job_id=job_id)
    saw_throttle = False
    for attempt in range(len(RETRY_WAITS) + 1):
        pulse({"description": job_id, "attempt": attempt + 1})
        with pace.slot("description") as slot:
            try:
                response = get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            except requests.RequestException:
                slot.neutral()
                response = None
            if response is not None:
                status = response.status_code
                if status == 200:
                    slot.clean()
                    text = extract_description(response.text)
                    return FragmentResult("ok", text) if text else FragmentResult("failed")
                if status in GONE_STATUSES:
                    slot.neutral()
                    return FragmentResult("gone")
                if status in THROTTLE_STATUSES:
                    slot.throttled_again() if saw_throttle else slot.throttled()
                    saw_throttle = True
                elif status in REFUSED_STATUSES:
                    slot.neutral()
                else:
                    slot.neutral()
                    return FragmentResult("failed")
        if attempt < len(RETRY_WAITS):
            sleep(RETRY_WAITS[attempt])
    return FragmentResult("refused")
