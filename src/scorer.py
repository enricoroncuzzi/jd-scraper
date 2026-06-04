from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field
from src.models import JobOffer, ScoredOffer

BATCH_SIZE = 10


class _ScoringItem(BaseModel):
    id: int
    score: int = Field(ge=1, le=10)
    comment: str = ""
    summary: str = ""


class _ScoringOutput(BaseModel):
    offers: list[_ScoringItem]


_SYSTEM = """You are a job scoring assistant. Score each job offer from 1 to 10 based on fit with the candidate profile.

Rules:
- Score 1-10: 10 = perfect fit, 1 = completely irrelevant
- If the description field is empty, assign score=1 and comment="Description unavailable — could not evaluate." and summary=""
- Boost score for offers containing priority keywords
- Lower score significantly for offers containing exclude keywords
- Return exactly the same number of offers you receive, preserving the id field"""

_HUMAN = """Candidate profile: {profile}

Priority keywords (boost score if present): {priority_keywords}
Exclude keywords (lower score significantly if present): {exclude_keywords}

Score these {count} job offers:
{offers}

Return all {count} offers. Each must have: id (same as input), score (1-10), comment (one sentence reason), summary (one sentence describing the role)."""


def _build_chain(groq_api_key: str):
    llm = ChatGroq(model="llama-3.3-70b-versatile", api_key=groq_api_key)
    return (
        ChatPromptTemplate.from_messages([
            ("system", _SYSTEM),
            ("human", _HUMAN),
        ])
        | llm.with_structured_output(_ScoringOutput)
    )


def _invoke_batch(chain, batch: list[JobOffer], profile: str, priority_keywords: list[str], exclude_keywords: list[str]) -> list[_ScoringItem]:
    offers_text = "\n\n".join(
        f"ID: {o.id}\nTitle: {o.title}\nCompany: {o.company}\n"
        f"Location: {o.location}\nDescription: {o.description or '(empty)'}"
        for o in batch
    )
    result: _ScoringOutput = chain.invoke({
        "profile": profile,
        "priority_keywords": ", ".join(priority_keywords),
        "exclude_keywords": ", ".join(exclude_keywords),
        "count": len(batch),
        "offers": offers_text,
    })
    return result.offers


def score_offers(
    offers: list[JobOffer],
    profile: str,
    priority_keywords: list[str],
    exclude_keywords: list[str],
    groq_api_key: str,
) -> list[ScoredOffer]:
    if not offers:
        return []

    chain = _build_chain(groq_api_key)

    all_scoring: list[_ScoringItem] = []
    for i in range(0, len(offers), BATCH_SIZE):
        batch = offers[i:i + BATCH_SIZE]
        print(f"[scorer] Scoring batch {i // BATCH_SIZE + 1}/{(len(offers) - 1) // BATCH_SIZE + 1} ({len(batch)} offers)...")
        all_scoring.extend(_invoke_batch(chain, batch, profile, priority_keywords, exclude_keywords))

    scoring_by_id = {s.id: s for s in all_scoring}
    return [
        ScoredOffer(
            **o.model_dump(),
            score=scoring_by_id[o.id].score,
            comment=scoring_by_id[o.id].comment,
            summary=scoring_by_id[o.id].summary,
        )
        for o in offers
        if o.id in scoring_by_id
    ]
