"""Per-day, per-provider language-model usage (spec section 6.5)."""
from dataclasses import dataclass
from datetime import date, datetime

from src.jobstore.db import utcnow


@dataclass(frozen=True)
class Usage:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


def utc_day(now: datetime | None = None) -> date:
    """Both providers reset on the UTC day boundary."""
    return (now or utcnow()).date()


def add_usage(conn, provider: str, *, requests: int = 1, input_tokens: int = 0,
              output_tokens: int = 0, now: datetime | None = None) -> None:
    """Count LLM requests against today's row. The caller commits."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO llm_budget (day, provider, requests, input_tokens, output_tokens)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (day, provider) DO UPDATE
               SET requests = llm_budget.requests + EXCLUDED.requests,
                   input_tokens = llm_budget.input_tokens + EXCLUDED.input_tokens,
                   output_tokens = llm_budget.output_tokens + EXCLUDED.output_tokens
            """,
            (utc_day(now), provider, requests, input_tokens or 0, output_tokens or 0),
        )


def used_today(conn, provider: str, now: datetime | None = None) -> Usage:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT requests, input_tokens, output_tokens FROM llm_budget "
            "WHERE day = %s AND provider = %s", (utc_day(now), provider))
        row = cur.fetchone()
    return Usage(*row) if row else Usage()


def stage_requests_today(conn, provider: str, stage: str, now: datetime | None = None) -> int:
    """Requests one stage made today (UTC), counted from llm_calls. The verifier's
    own daily share is measured per stage, not per provider."""
    day = utc_day(now)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM llm_calls WHERE provider = %s AND stage = %s "
            "AND (started_at AT TIME ZONE 'UTC')::date = %s", (provider, stage, day))
        return cur.fetchone()[0]
