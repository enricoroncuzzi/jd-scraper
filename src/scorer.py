from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from src.models import JobOffer, ScoredOffer, RankedOffers

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
        | llm.with_structured_output(RankedOffers)
    )


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

    offers_text = "\n\n".join(
        f"ID: {o.id}\nTitle: {o.title}\nCompany: {o.company}\n"
        f"Location: {o.location}\nDescription: {o.description or '(empty)'}"
        for o in offers
    )

    result: RankedOffers = chain.invoke({
        "profile": profile,
        "priority_keywords": ", ".join(priority_keywords),
        "exclude_keywords": ", ".join(exclude_keywords),
        "count": len(offers),
        "offers": offers_text,
    })

    scored_by_id = {s.id: s for s in result.offers}
    return [scored_by_id[o.id] for o in offers if o.id in scored_by_id]
