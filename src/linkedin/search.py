"""One LinkedIn guest search request, with the spec's throttle handling.

The endpoint honours only keywords, location, f_TPR and start. f_WT, f_E, f_JT
and sortBy are silently ignored, so none of them is ever sent.
"""
import time
from dataclasses import dataclass

import requests

from src.linkedin.cards import Card, parse_cards

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}
REQUEST_TIMEOUT = 20
# Waits between retries of the same request: 5 s, 15 s, 45 s, then give up.
RETRY_WAITS = (5, 15, 45)
THROTTLE_STATUSES = frozenset({429, 999})
TRANSIENT_STATUSES = frozenset({503, 504})


class SearchThrottled(RuntimeError):
    """LinkedIn kept answering 429/999 through every retry."""


class SearchFailed(RuntimeError):
    """A non-retryable status, or a transient one that never recovered."""


@dataclass(frozen=True)
class Page:
    cards: list[Card]
    throttled: bool  # a throttle status was seen while getting this page
    end: bool = False  # HTTP 400: a start offset past the end of the results


def fetch_page(pace, *, role: str, area: str, f_tpr_value: str, start: int,
               get=requests.get, sleep=time.sleep) -> Page:
    params = {"keywords": role, "location": area, "f_TPR": f_tpr_value, "start": start}
    saw_throttle = False
    last_problem = "no attempt"
    for attempt in range(len(RETRY_WAITS) + 1):
        with pace.slot("search") as slot:
            try:
                response = get(SEARCH_URL, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                slot.neutral()
                last_problem = f"network error: {exc}"
                response = None
            if response is not None:
                status = response.status_code
                if status == 200:
                    slot.clean()
                    return Page(parse_cards(response.text), saw_throttle)
                if status == 400:
                    slot.neutral()
                    return Page([], saw_throttle, end=True)
                if status in THROTTLE_STATUSES:
                    slot.throttled()
                    saw_throttle = True
                    last_problem = f"HTTP {status}"
                elif status in TRANSIENT_STATUSES:
                    slot.neutral()
                    last_problem = f"HTTP {status}"
                else:
                    slot.neutral()
                    raise SearchFailed(f"LinkedIn search returned HTTP {status}")
        if attempt < len(RETRY_WAITS):
            sleep(RETRY_WAITS[attempt])
    if saw_throttle:
        raise SearchThrottled(last_problem)
    raise SearchFailed(last_problem)
