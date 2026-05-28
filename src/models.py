from pydantic import BaseModel, Field


class JobOffer(BaseModel):
    id: int
    title: str
    company: str
    location: str = "N/A"
    link: str
    description: str = ""


class ScoredOffer(JobOffer):
    score: int = Field(ge=1, le=10)
    comment: str = ""
    summary: str = ""


class RankedOffers(BaseModel):
    offers: list[ScoredOffer]
