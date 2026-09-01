import time
import openai
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.callbacks.base import BaseCallbackHandler
from pydantic import BaseModel, Field
from src.models import JobOffer, ScoredOffer

BATCH_SIZE = 5
_MAX_DESC_CHARS = 5000  # truncate only in LLM prompt; full description preserved in JobOffer


class _ScoringItem(BaseModel):
    id: int
    score: int = Field(ge=1, le=10)
    comment: str = ""
    summary: str = ""


class _ScoringOutput(BaseModel):
    offers: list[_ScoringItem]


class _TokenCounter(BaseCallbackHandler):
    def __init__(self):
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def on_llm_end(self, response, **kwargs):
        usage = (response.llm_output or {}).get("token_usage", {})
        self.prompt_tokens += usage.get("prompt_tokens", 0)
        self.completion_tokens += usage.get("completion_tokens", 0)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


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


_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_OPENROUTER_MODEL = "nvidia/nemotron-3-nano-30b-a3b:free"
# OpenRouter's native fallback mechanism: if the primary model is busy/rate-limited,
# OpenRouter itself retries the request against the next model in this list before
# ever returning an error to us. Order picked from empirical latency/reliability
# testing against this repo's real scoring prompt (see PR description for numbers).
# All entries verified to support tool-calling ("tools" in supported_parameters
# on OpenRouter's /api/v1/models) as of 2026-09-01, since scoring uses
# with_structured_output(method="function_calling") below.
# nvidia/nemotron-3-ultra-550b-a55b:free is kept last-resort-only: ~56s observed
# latency makes it a poor early fallback for a batch-scoring pipeline.
_OPENROUTER_FALLBACK_MODELS = [
    "nvidia/nemotron-3-super-120b-a12b:free",
    "poolside/laguna-xs-2.1:free",
    "liquid/lfm-2.5-2.6b:free",
    "cohere/north-mini-code:free",
    "z-ai/glm-5.2:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
]

# OpenRouter's 429 body has no Cerebras-style string "code" to tell a same-day quota
# exhaustion apart from a transient per-minute throttle - both come back as
# {"code": 429, ...}. The reliable signal is how far away X-RateLimit-Reset is: a
# per-minute/upstream throttle resets within seconds, a daily free-tier cap resets
# at the next UTC day boundary (hours away). Empirically verified against a live key.
_QUOTA_RESET_THRESHOLD_SECONDS = 90


# OpenRouter sometimes returns an upstream 5xx/timeout as an HTTP 200 with a
# JSON error body instead of an actual 5xx status (confirmed via the 2026-09-01
# outage traceback and reproduced against a mocked transport). Because the
# status is 200, the openai SDK never raises openai.InternalServerError -
# langchain_openai's _create_chat_result sees the "error" key in the parsed
# body and raises a plain ValueError instead. Inspect the body to tell that
# case apart from an unrelated ValueError (e.g. a genuinely malformed
# response) so only the retryable case gets the batch-retry treatment.
_RETRYABLE_STATUS_CODES = {500, 502, 503, 504}
_RETRYABLE_MESSAGE_KEYWORDS = ("timeout", "timed out", "gateway", "unavailable")


def _is_retryable_upstream_value_error(e: ValueError) -> bool:
    if not e.args:
        return False
    body = e.args[0]
    if isinstance(body, dict):
        code = body.get("code")
        if isinstance(code, int) and code in _RETRYABLE_STATUS_CODES:
            return True
        message = str(body.get("message", ""))
    else:
        message = str(body)
    message = message.lower()
    return any(keyword in message for keyword in _RETRYABLE_MESSAGE_KEYWORDS)


def _is_quota_exceeded(e: openai.RateLimitError) -> bool:
    body = e.body or {}
    if body.get("code") == "token_quota_exceeded":  # legacy Cerebras shape
        return True
    headers = getattr(getattr(e, "response", None), "headers", None) or {}
    reset_ms = headers.get("x-ratelimit-reset")
    if reset_ms is None:
        reset_ms = (body.get("metadata") or {}).get("headers", {}).get("X-RateLimit-Reset")
    if reset_ms is None:
        return False
    try:
        return int(reset_ms) / 1000 - time.time() > _QUOTA_RESET_THRESHOLD_SECONDS
    except (TypeError, ValueError):
        return False


def _build_chain(llm_api_key: str):
    llm = ChatOpenAI(
        model=_OPENROUTER_MODEL,
        api_key=llm_api_key,
        base_url=_OPENROUTER_BASE_URL,
        extra_body={"models": _OPENROUTER_FALLBACK_MODELS},
    )
    return (
        ChatPromptTemplate.from_messages([
            ("system", _SYSTEM),
            ("human", _HUMAN),
        ])
        # method="function_calling" forces tool_choice to this schema's function
        # (vs. the default "json_schema"/response_format method, which isn't
        # supported by every free OpenRouter model and produced noticeably
        # bloated completions in testing). Forced tool-calling is the only mode
        # validated clean (no reasoning-token bloat) across the primary + fallback
        # models below.
        | llm.with_structured_output(_ScoringOutput, method="function_calling")
    )


def _invoke_batch(chain, batch: list[JobOffer], profile: str, priority_keywords: list[str], exclude_keywords: list[str], counter: _TokenCounter, max_retries: int = 10) -> list[_ScoringItem]:
    offers_text = "\n\n".join(
        f"ID: {o.id}\nTitle: {o.title}\nCompany: {o.company}\n"
        f"Location: {o.location}\nDescription: {o.description[:_MAX_DESC_CHARS] or '(empty)'}"
        for o in batch
    )
    payload = {
        "profile": profile,
        "priority_keywords": ", ".join(priority_keywords),
        "exclude_keywords": ", ".join(exclude_keywords),
        "count": len(batch),
        "offers": offers_text,
    }
    for attempt in range(max_retries):
        try:
            result: _ScoringOutput = chain.invoke(payload, config={"callbacks": [counter]})
            return result.offers
        except openai.RateLimitError as e:
            if _is_quota_exceeded(e):
                raise  # daily quota — no point retrying, propagate immediately
            if attempt == max_retries - 1:
                raise
            wait = min(10 * (2 ** attempt), 300)  # 10s → 20 → 40 → 80 → 160 → 300s cap
            print(f"[scorer] Rate limited, retrying in {wait}s (attempt {attempt + 1}/{max_retries})...")
            time.sleep(wait)
        except (openai.InternalServerError, openai.APIConnectionError) as e:
            # openai.InternalServerError covers real 5xx statuses (including
            # timeout-shaped ones OpenRouter surfaces as an actual HTTP 5xx);
            # openai.APIConnectionError also covers openai.APITimeoutError
            # (its subclass) for genuine network-level timeouts.
            if attempt == max_retries - 1:
                raise
            wait = min(10 * (2 ** attempt), 300)
            print(f"[scorer] Upstream error ({type(e).__name__}), retrying in {wait}s (attempt {attempt + 1}/{max_retries})...")
            time.sleep(wait)
        except ValueError as e:
            if not _is_retryable_upstream_value_error(e):
                raise
            if attempt == max_retries - 1:
                raise
            wait = min(10 * (2 ** attempt), 300)
            print(f"[scorer] Upstream error (5xx via 200 error body), retrying in {wait}s (attempt {attempt + 1}/{max_retries})...")
            time.sleep(wait)


def score_offers(
    offers: list[JobOffer],
    profile: str,
    priority_keywords: list[str],
    exclude_keywords: list[str],
    llm_api_key: str,
) -> tuple[list[ScoredOffer], dict]:
    if not offers:
        return [], {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    chain = _build_chain(llm_api_key)
    counter = _TokenCounter()
    total_batches = (len(offers) - 1) // BATCH_SIZE + 1

    all_scoring: list[_ScoringItem] = []
    for i in range(0, len(offers), BATCH_SIZE):
        batch = offers[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        print(f"[scorer] Scoring batch {batch_num}/{total_batches} ({len(batch)} offers)...")
        try:
            all_scoring.extend(_invoke_batch(chain, batch, profile, priority_keywords, exclude_keywords, counter))
        except openai.RateLimitError as e:
            if _is_quota_exceeded(e):
                print(f"[scorer] Daily token quota exhausted at batch {batch_num}/{total_batches}. Saving {len(all_scoring)} scored offers.")
            else:
                print(f"[scorer] Rate limit retries exhausted at batch {batch_num}/{total_batches}. Saving {len(all_scoring)} scored offers.")
            break
        except (openai.InternalServerError, openai.APIConnectionError):
            print(f"[scorer] Upstream error retries exhausted at batch {batch_num}/{total_batches}. Saving {len(all_scoring)} scored offers.")
            break
        except ValueError as e:
            if not _is_retryable_upstream_value_error(e):
                raise  # not a retryable upstream error — an unexpected bug, propagate as before
            print(f"[scorer] Upstream error retries exhausted at batch {batch_num}/{total_batches}. Saving {len(all_scoring)} scored offers.")
            break

    scoring_by_id = {s.id: s for s in all_scoring}
    scored = [
        ScoredOffer(
            **o.model_dump(),
            score=scoring_by_id[o.id].score,
            comment=scoring_by_id[o.id].comment,
            summary=scoring_by_id[o.id].summary,
        )
        for o in offers
        if o.id in scoring_by_id
    ]
    usage = {
        "prompt_tokens": counter.prompt_tokens,
        "completion_tokens": counter.completion_tokens,
        "total_tokens": counter.total_tokens,
    }
    return scored, usage
