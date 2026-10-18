from typing import Literal
from pydantic import BaseModel, Field


class JobOffer(BaseModel):
    id: int
    title: str
    company: str
    location: str = "N/A"
    link: str
    description: str = ""
    description_status: Literal["ok", "partial", "failed", "rate_limited"] = "ok"
    work_mode: str = ""
    # See src/remote_verdicts.py for what each verdict means. `unconfirmed` is the
    # pre-2026-10-18 value and stays accepted so old rows and recorded outputs
    # still parse; it is read as not_stated everywhere.
    remote_verdict: Literal[
        "confirmed", "advised", "not_stated", "rejected", "not_checked", "unconfirmed"
    ] = "not_checked"
    remote_reason: str = ""
    remote_advice: str = ""


class ScoredOffer(JobOffer):
    score: int = Field(ge=1, le=10)
    comment: str = ""
    summary: str = ""


class RankedOffers(BaseModel):
    offers: list[ScoredOffer]
