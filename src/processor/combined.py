"""Spec 6.4: one structured call that returns the remote verdict AND the score.

Gated: processor.json `merged_call.enabled` ships false. It is adopted only
after scripts/llm_experiment.py shows it agrees with the two-call pipeline on a
fixed sample (verdict agreement above 95 percent, scores within 1 for at least
90 percent of jobs). The rules text is the verifier's and the scorer's own, so
the judgement criteria do not drift; only the packaging changes.
"""
import time
from datetime import datetime

import openai
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field, field_validator

from src import telemetry
from src.processor.config import ProcessorConfig
from src.processor.llm_stages import (
    LlmKeys, budget_open, due_batch, retry_delay, to_offers, waiting_rows,
)
from src.processor.llm_sink import batch_context
from src.processor.state import defer, set_stage
from src.remote_verifier import (
    _ITALY_RULE, _OPENROUTER_BASE_URL, _OPENROUTER_FALLBACK_MODELS, _OPENROUTER_MODEL,
    _REMOTE_ONLY_RULE, _extract_policy_excerpt,
)
from src.scorer import (
    _MAX_DESC_CHARS, _EmptyStructuredOutput, _TokenCounter, _is_quota_exceeded,
    _is_retryable_upstream_value_error, _openrouter_quota_exhausted,
)

_MAX_RETRIES = 6

_SYSTEM = """You evaluate job offers for one candidate. For each offer give two independent judgements.

1. Remote verdict. {remote_rule}
   unconfirmed when the description simply does not settle the question. Never guess: if the text is silent or vague, answer unconfirmed rather than confirmed or rejected. The reason is one sentence naming the phrase in the description that drove the verdict.

2. Fit score from 1 to 10 against the candidate profile: 10 = perfect fit, 1 = completely irrelevant.
   - If the description field is empty, assign score=1 and comment="Description unavailable, could not evaluate." and summary=""
   - Boost the score for offers containing priority keywords
   - Lower the score significantly for offers containing exclude keywords
   The score does not depend on the remote verdict.

Return exactly the same number of offers you receive, preserving the id field."""

_HUMAN = """Candidate profile: {profile}

Priority keywords (boost score if present): {priority_keywords}
Exclude keywords (lower score significantly if present): {exclude_keywords}

Evaluate these {count} job offers:
{offers}

Return all {count} offers. Each must have: id (same as input), verdict (confirmed, rejected or unconfirmed), reason (one sentence), score (1-10), comment (one sentence reason for the score), summary (one sentence describing the role)."""


class CombinedItem(BaseModel):
    id: int
    verdict: str
    reason: str = ""
    score: int = Field(ge=1, le=10)
    comment: str = ""
    summary: str = ""

    @field_validator("verdict")
    @classmethod
    def _canonical(cls, value: str) -> str:
        return value.strip().lower()


class CombinedOutput(BaseModel):
    offers: list[CombinedItem]


_PROMPT_VERSION = telemetry.prompt_version(_SYSTEM, _HUMAN)


def build_chain(llm_api_key: str):
    llm = ChatOpenAI(
        model=_OPENROUTER_MODEL, api_key=llm_api_key, base_url=_OPENROUTER_BASE_URL,
        extra_body={"models": _OPENROUTER_FALLBACK_MODELS}, temperature=0.0, max_retries=0)
    return (
        ChatPromptTemplate.from_messages([("system", _SYSTEM), ("human", _HUMAN)])
        | llm.with_structured_output(CombinedOutput, method="function_calling")
    )


def offer_text(offer) -> str:
    """The scorer's description prefix, plus the verifier's policy passages when
    they sit beyond that prefix (some postings state the work mode very late)."""
    description = offer.description
    shown = description[:_MAX_DESC_CHARS]
    excerpt = _extract_policy_excerpt(description)
    if excerpt and excerpt not in shown and len(description) > _MAX_DESC_CHARS:
        shown = f"{shown}\n[Work-location passages from later in the text] {excerpt}"
    return (f"ID: {offer.id}\nTitle: {offer.title}\nCompany: {offer.company}\n"
            f"Location: {offer.location}\nDescription: {shown or '(empty)'}")


def invoke_combined(chain, offers, *, require_italy_eligibility: bool, profile: str,
                    priority_keywords: list[str], exclude_keywords: list[str],
                    sleep=time.sleep) -> list[CombinedItem]:
    payload = {
        "remote_rule": _ITALY_RULE if require_italy_eligibility else _REMOTE_ONLY_RULE,
        "profile": profile, "priority_keywords": ", ".join(priority_keywords),
        "exclude_keywords": ", ".join(exclude_keywords), "count": len(offers),
        "offers": "\n\n".join(offer_text(offer) for offer in offers),
    }
    counter = _TokenCounter()
    for attempt in range(_MAX_RETRIES):
        try:
            counter.begin_call()
            with telemetry.llm_call(stage="combined", provider="openrouter",
                                    request_model=_OPENROUTER_MODEL, batch_size=len(offers),
                                    attempt=attempt + 1, prompt_version=_PROMPT_VERSION,
                                    is_quota_exhausted=_openrouter_quota_exhausted) as call:
                result = chain.invoke(payload, config={"callbacks": [counter]})
                call.set_usage(response_model=counter.last_model,
                               input_tokens=counter.last_prompt_tokens,
                               output_tokens=counter.last_completion_tokens)
                if result is None:
                    raise _EmptyStructuredOutput()
            return result.offers
        except openai.RateLimitError as exc:
            if _is_quota_exceeded(exc) or attempt == _MAX_RETRIES - 1:
                raise
        except (_EmptyStructuredOutput, openai.InternalServerError, openai.APIConnectionError):
            if attempt == _MAX_RETRIES - 1:
                raise
        except ValueError as exc:
            if not _is_retryable_upstream_value_error(exc) or attempt == _MAX_RETRIES - 1:
                raise
        sleep(min(10 * 2 ** attempt, 120))
    raise RuntimeError("unreachable")


def run_combined_batches(conn, cfg: ProcessorConfig, now: datetime, keys: LlmKeys, *,
                         chain=None, sleep=time.sleep, max_batches: int = 3) -> int:
    """Verdict and score in one call for tiers that run the remote check. Returns
    how many jobs were scored."""
    done = 0
    chain = chain or build_chain(keys.llm_api_key)
    tiers_by_key: dict[tuple, list[int]] = {}
    for tier, policy in sorted(cfg.policies.items()):
        if policy.remote_check_enabled:
            key = (policy.require_italy_eligibility, policy.scoring_key)
            tiers_by_key.setdefault(key, []).append(tier)
    for _ in range(max_batches):
        if not budget_open(conn, cfg, now):
            break
        batch = None
        for (require_italy, _scoring_key), tiers in tiers_by_key.items():
            with conn.cursor() as cur:
                rows = waiting_rows(cur, "remote_verdict IS NULL AND score IS NULL AND tier = ANY(%s)",
                                    (tiers,), now)
            batch = due_batch(rows, cfg.scoring_batch_size, cfg.flush_after_minutes, now)
            if batch:
                policy = cfg.policies[tiers[0]]
                break
        if not batch:
            break
        try:
            with batch_context(row.id for row in batch):
                items = invoke_combined(
                    chain, to_offers(batch), require_italy_eligibility=require_italy,
                    profile=policy.candidate_profile, priority_keywords=list(policy.priority_keywords),
                    exclude_keywords=list(policy.exclude_keywords), sleep=sleep)
        except (openai.OpenAIError, ValueError, _EmptyStructuredOutput) as exc:
            print(f"[processor] combined call failed ({type(exc).__name__}); batch will retry")
            items = []
        by_index = {item.id: item for item in items if item.verdict in
                    ("confirmed", "rejected", "unconfirmed")}
        with conn.cursor() as cur:
            for index, row in enumerate(batch):
                item = by_index.get(index)
                if item is None:
                    defer(cur, row.id, now, retry_delay(row.attempts))
                    continue
                cur.execute(
                    "UPDATE jobs SET remote_verdict = %s, remote_reason = %s, score = %s, "
                    "comment = %s, summary = %s WHERE id = %s",
                    (item.verdict, item.reason, item.score, item.comment, item.summary, row.id))
                if item.verdict == "rejected":
                    set_stage(cur, row.id, "filtered_out", "remote_rejected", now)
                else:
                    set_stage(cur, row.id, "scored", "", now)
                done += 1
        conn.commit()
        if len(by_index) < len(batch):
            break
    return done
