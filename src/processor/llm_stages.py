"""Shared plumbing for the two language-model stages: choosing which waiting jobs
form a batch (on size, or once the oldest has waited long enough), the JobOffer
adapter the existing verifier and scorer expect, and the day's budget guard."""
import os
from dataclasses import dataclass
from datetime import datetime, timedelta

from src.jobstore import budget
from src.models import JobOffer
from src.processor.config import ProcessorConfig
from src.processor.state import SELECT_JOB, JobRow


@dataclass(frozen=True)
class LlmKeys:
    llm_api_key: str
    groq_api_key: str = ""

    @classmethod
    def from_env(cls) -> "LlmKeys":
        return cls(os.environ["LLM_API_KEY"], os.environ.get("GROQ_API_KEY", ""))


def to_offers(rows: list[JobRow]) -> list[JobOffer]:
    """The existing stages key results by a small integer id, so the batch is
    numbered 0..n-1 and results are mapped back by position."""
    return [
        JobOffer(id=index, title=row.title, company=row.company, location=row.location_raw,
                 link=row.canonical_url, description=row.description, description_status="ok",
                 work_mode=row.work_mode)
        for index, row in enumerate(rows)
    ]


def budget_open(conn, cfg: ProcessorConfig, now: datetime) -> bool:
    """False once today's OpenRouter requests reach the stop line, which leaves
    room under the 1,000/day account limit for tailoring."""
    return budget.used_today(conn, "openrouter", now).requests < cfg.llm_stop_at_requests


def due_batch(rows: list[JobRow], batch_size: int, flush_after_minutes: int,
              now: datetime) -> list[JobRow]:
    """A full batch, or everything waiting once the oldest job has waited
    flush_after_minutes. Empty when neither holds."""
    if len(rows) >= batch_size:
        return rows[:batch_size]
    if rows and min(row.stage_changed_at for row in rows) <= now - timedelta(minutes=flush_after_minutes):
        return rows
    return []


def waiting_rows(cur, condition: str, params: tuple, now: datetime) -> list[JobRow]:
    cur.execute(
        f"{SELECT_JOB} WHERE stage = 'described' AND next_attempt_at <= %s AND {condition} "
        "ORDER BY tier, first_seen_at", (now, *params))
    return [JobRow(*row) for row in cur.fetchall()]


def retry_delay(attempts: int, first_minutes: int = 5, cap_minutes: int = 60) -> timedelta:
    return timedelta(minutes=min(first_minutes * 2 ** attempts, cap_minutes))
