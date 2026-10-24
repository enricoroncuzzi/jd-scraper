"""Per-request accounting for the collectors (2026-10-24 build).

LinkedIn's requests are already one row each in `pace_events`; that table is the LinkedIn
record, and this module only reads it. freehire's and Himalayas' requests are written here,
one row per HTTP attempt, as they go out (src/sources/base.py `JsonClient.get_json`).

Nothing in this module sends, retries, paces or filters a request: it is observation only,
and every write is wrapped so a recording fault can never break a collection run.
"""
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from src.jobstore.db import connect, store_url, utcnow

SOURCES = ("linkedin", "freehire", "himalayas")

# A failed request can have no status at all (a connection error or a timeout). A successful
# LinkedIn request can too: the packaging link check calls `slot.clean()` with no status.
NO_STATUS = "none"
OK_WITHOUT_STATUS = "nocode"

# What counts as a refusal answer. LinkedIn stores a 403 as outcome `neutral` (and 999 as
# `throttled`), so the status has to be read as well as the outcome.
REFUSAL_STATUSES = frozenset({403, 429})
LINKEDIN_REFUSAL_STATUSES = frozenset({403, 429, 999})
REFUSALS_BY_SOURCE = {"linkedin": LINKEDIN_REFUSAL_STATUSES,
                      "freehire": REFUSAL_STATUSES,
                      "himalayas": REFUSAL_STATUSES}

SOURCE_REQUEST_OUTCOMES = ("ok", "refused", "error")


def outcome_for_status(status: int | None) -> str:
    """ok for any 2xx; refused when the site said no (403, 429); error for everything else,
    including a connection error or timeout with no status at all."""
    if status is None:
        return "error"
    if 200 <= status < 300:
        return "ok"
    return "refused" if status in REFUSAL_STATUSES else "error"


def record_request(conn, source: str, at: datetime, status: int | None) -> None:
    """Append one request row. The caller owns the commit."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO source_requests (at, source, outcome, status) VALUES (%s, %s, %s, %s)",
            (at, source, outcome_for_status(status), status))


class SourceRequestLog:
    """The recorder the stage-2 collector hands to JsonClient: `record(status)` once per
    HTTP attempt. Each call opens its own short-lived connection, so a broken record can
    never poison or hold the walk's transaction. Every failure is logged and swallowed:
    telemetry must not break or slow a collection run."""

    def __init__(self, source: str, connect_fn: Callable[[], object] | None = None,
                 now: Callable[[], datetime] = utcnow):
        self.source = source
        self.connect_fn = connect_fn or (lambda: connect(store_url()))
        self.now = now

    def __call__(self, status: int | None) -> None:
        try:
            conn = self.connect_fn()
            try:
                record_request(conn, self.source, self.now(), status)
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - recording must never break a collection run
            print(f"[collect-{self.source}] could not record a request: "
                  f"{type(exc).__name__}: {exc}")


@dataclass(frozen=True)
class DayRequests:
    """One source's requests and new jobs for one Rome day."""

    source: str
    day: date
    sent: int
    ok: int
    refused: int
    errors: int
    new_jobs: int
    codes: tuple[tuple[str, int], ...]

    @property
    def codes_text(self) -> str:
        return " ".join(f"{code}:{count}" for code, count in self.codes) or "-"


def _code(status: int | None, outcome: str) -> str:
    if status is not None:
        return str(status)
    return OK_WITHOUT_STATUS if outcome == "ok" else NO_STATUS


def _code_order(code: str) -> tuple[int, int]:
    if code.isdigit():
        return (0, int(code))
    return (1, 0) if code == OK_WITHOUT_STATUS else (2, 0)


def _fold(totals: dict, rows: Sequence[tuple]) -> None:
    """Fold (source, day, outcome, status, count) rows into per-(source, day) totals."""
    for source, day, outcome, status, count in rows:
        refusals = REFUSALS_BY_SOURCE.get(source, REFUSAL_STATUSES)
        bucket = totals.setdefault((source, day),
                                   {"sent": 0, "ok": 0, "refused": 0, "codes": {}})
        bucket["sent"] += count
        if outcome == "ok":
            bucket["ok"] += count
        elif outcome == "refused" or status in refusals:
            bucket["refused"] += count
        label = _code(status, outcome)
        bucket["codes"][label] = bucket["codes"].get(label, 0) + count


def daily_requests(conn, first_day: date, last_day: date, *, zone: str = "Europe/Rome",
                   sources: Sequence[str] = SOURCES) -> list[DayRequests]:
    """Every source's requests and new jobs per Rome day, `first_day` to `last_day`
    inclusive. One row per (source, day), zeros included, in `sources` order then day
    ascending: a source that went silent shows a zero row instead of disappearing."""
    lo = datetime.combine(first_day, time.min, tzinfo=ZoneInfo(zone))
    hi = datetime.combine(last_day + timedelta(days=1), time.min, tzinfo=ZoneInfo(zone))
    totals: dict[tuple[str, date], dict] = {}
    with conn.cursor() as cur:
        if "linkedin" in sources:
            # pace_events holds the LinkedIn requests by construction: the pace it records
            # for is the LinkedIn one, and no other source writes there.
            cur.execute(
                "SELECT 'linkedin', (at AT TIME ZONE %s)::date, outcome, status, count(*) "
                "FROM pace_events WHERE at >= %s AND at < %s GROUP BY 2, 3, 4",
                (zone, lo, hi))
            _fold(totals, cur.fetchall())
        others = [source for source in sources if source != "linkedin"]
        if others:
            cur.execute(
                "SELECT source, (at AT TIME ZONE %s)::date, outcome, status, count(*) "
                "FROM source_requests WHERE at >= %s AND at < %s AND source = ANY(%s) "
                "GROUP BY 1, 2, 3, 4", (zone, lo, hi, others))
            _fold(totals, cur.fetchall())
        cur.execute(
            "SELECT source, (seen_at AT TIME ZONE %s)::date, count(*) FROM sightings "
            "WHERE seen_at >= %s AND seen_at < %s AND source = ANY(%s) GROUP BY 1, 2",
            (zone, lo, hi, list(sources)))
        new_jobs = {(source, day): count for source, day, count in cur.fetchall()}
    rows = []
    for source in sources:
        for offset in range((last_day - first_day).days + 1):
            day = first_day + timedelta(days=offset)
            bucket = totals.get((source, day),
                                {"sent": 0, "ok": 0, "refused": 0, "codes": {}})
            sent, ok, refused = bucket["sent"], bucket["ok"], bucket["refused"]
            rows.append(DayRequests(
                source, day, sent, ok, refused, sent - ok - refused,
                new_jobs.get((source, day), 0),
                tuple(sorted(bucket["codes"].items(), key=lambda item: _code_order(item[0])))))
    return rows
