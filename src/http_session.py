"""Shared HTTP helpers for the polite collectors (design section 7).

`new_session` returns one reused `requests.Session` per source. Its cookie jar
refuses every cookie, so a reused connection sends exactly the headers a bare
`requests.get` sends: no cookies are ever sent back to LinkedIn, freehire or
Himalayas. LinkedIn's own request headers are not changed in any way.

`retry_after_seconds` reads a `Retry-After` header value as a number of seconds,
either a whole-second count or an HTTP-date; anything else is None. Past dates
are 0, and there is no cap here - each caller applies its own bound.
"""
import http.cookiejar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests
import requests.cookies


def new_session() -> requests.Session:
    """A reused session whose cookie jar accepts no cookie from any domain."""
    session = requests.Session()
    session.cookies = requests.cookies.RequestsCookieJar(
        policy=http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
    return session


def retry_after_seconds(value, now: datetime) -> float | None:
    """The `Retry-After` header as seconds from `now`, or None when it is missing,
    non-numeric, an unreadable date, or negative."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.isdigit():
        return float(text)
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (parsed - now).total_seconds())
