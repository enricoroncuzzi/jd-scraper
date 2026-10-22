"""One LinkedIn guest search request, with the spec's refusal handling.

The endpoint honours only keywords, location, f_TPR and start. f_WT, f_E, f_JT
and sortBy are silently ignored, so none of them is ever sent.

A 429/999 is one refusal and returns at once: the host-level wait in the pace
applies before the next request. The 5/15/45 s ladder is for neutral failures
only. A 200 with no cards while `distrust_empty` is set (a page asked right
after a refusal) is counted as a refusal too and not trusted as the end of the
results.
"""
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from src.http_session import retry_after_seconds
from src.linkedin.captcha import looks_like_captcha
from src.linkedin.cards import Card, parse_cards
from src.services.pulse import pulse

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
    """LinkedIn answered 429/999, or an empty page right after a refusal."""


class SearchFailed(RuntimeError):
    """A non-retryable status, or a transient one that never recovered."""


@dataclass(frozen=True)
class Page:
    cards: list[Card]
    throttled: bool  # this response was counted as a refusal
    end: bool = False  # HTTP 400: a start offset past the end of the results


def fetch_page(pace, *, role: str, area: str, f_tpr_value: str, start: int,
               distrust_empty: bool = False, get=requests.get, sleep=time.sleep) -> Page:
    params = {"keywords": role, "location": area, "f_TPR": f_tpr_value, "start": start}
    last_problem = "no attempt"
    for attempt in range(len(RETRY_WAITS) + 1):
        pulse({"search": f"{role} / {area}", "start": start, "attempt": attempt + 1})
        outcome = None
        with pace.slot("search") as slot:
            try:
                response = get(SEARCH_URL, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            except requests.ConnectTimeout as exc:
                slot.neutral()
                last_problem = f"network error: {exc}"
                response = None
            except requests.Timeout as exc:
                slot.neutral()
                last_problem = f"network error: {exc}"
                response = None
            except requests.ConnectionError as exc:
                slot.neutral()
                slot.block("connection")
                last_problem = f"network error: {exc}"
                response = None
            except requests.RequestException as exc:
                slot.neutral()
                last_problem = f"network error: {exc}"
                response = None
            if response is not None:
                status = response.status_code
                if status == 200:
                    cards = parse_cards(response.text)
                    if not cards and looks_like_captcha(response.text):
                        slot.neutral(200)
                        slot.block("captcha")
                        outcome = ("raise", SearchFailed("LinkedIn CAPTCHA page"))
                    elif distrust_empty and not cards:
                        slot.throttled(status)
                        outcome = ("return", Page([], throttled=True))
                    else:
                        slot.clean(status)
                        outcome = ("return", Page(cards, throttled=False))
                elif status == 400:
                    slot.neutral(status)
                    outcome = ("return", Page([], throttled=False, end=True))
                elif status in THROTTLE_STATUSES:
                    retry_after = retry_after_seconds(
                        response.headers.get("Retry-After"), datetime.now(timezone.utc))
                    slot.throttled(status, retry_after=retry_after)
                    if status == 999:
                        slot.block("http_999")
                    last_problem = f"HTTP {status}"
                    outcome = ("raise", SearchThrottled(last_problem))
                elif status == 403:
                    slot.neutral(status)
                    slot.block("http_403")
                    outcome = ("raise", SearchFailed("LinkedIn search returned HTTP 403"))
                elif status in TRANSIENT_STATUSES:
                    slot.neutral(status)
                    last_problem = f"HTTP {status}"
                else:
                    slot.neutral(status)
                    outcome = ("raise", SearchFailed(f"LinkedIn search returned HTTP {status}"))
        if outcome is not None:
            action, value = outcome
            if action == "return":
                return value
            raise value
        if attempt < len(RETRY_WAITS):
            sleep(RETRY_WAITS[attempt])
    raise SearchFailed(last_problem)
