"""Confirm that an offer is genuinely full-remote before it costs scoring tokens.

LinkedIn's "remote" work-mode filter is not trustworthy for this: plenty of
listings are tagged remote but require residency in the posting country. This
stage reads the description itself and rules on it.

It runs on Groq rather than OpenRouter as the PRIMARY provider, deliberately:
scoring already exhausts the OpenRouter free-tier daily cap on bad days, and
that has taken whole runs down; putting a second stage on the same key would
make that worse. Groq is a separate key with a separate quota, already a
dependency of the CV tailoring path.

Groq's 200,000-token/day account-wide budget is real and gets hit on busy
days (measured peak: 78% of it on one tier alone). When it runs out mid-run -
or Groq is unusable for any other reason (no key, a broken client) - this
stage fails over to OpenRouter rather than marking the rest of the day's
offers "unconfirmed", and stops calling whichever provider is known
exhausted for the rest of the run rather than repeating a doomed call per
batch. See _openrouter_chain and GROQ_DAILY_TOKEN_LIMIT below.
OpenRouter's own free-tier constraint (daily REQUEST cap, not tokens) is why
this is safe only as a fallback, not as the primary: see
_OPENROUTER_FALLBACK_MODELS' comment and src/scorer.py, which already spends
part of that budget on scoring - and why a RateLimitError from OpenRouter
during the fallback gets the same "stop hammering it" treatment Groq's TPD
case does, rather than repeating a doomed request for every remaining batch.

The stage is a filter, never a gate. Every failure mode - an empty
description, a batch that will not complete, both providers unavailable -
resolves to "unconfirmed", so a bad API minute can never silently discard a
real job. What changed is that "the stage quietly failing" now surfaces as
usage["degraded"] whenever a material share of batches never produced a real
verdict, not only when literally every batch failed (see
_DEGRADED_FAILURE_RATIO) - a run where 27 of 28 batches died used to report
itself healthy.
"""

import functools
import inspect
import json
import time

import groq
import openai
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ValidationError, field_validator

from src import telemetry
from src.llm_limits import limit_for
from src.models import JobOffer
from src.scorer import (
    _EmptyStructuredOutput,
    _is_quota_exceeded,
    _is_retryable_upstream_value_error,
    _openrouter_quota_exhausted,
    _TokenCounter,
)

# Larger than the scorer's BATCH_SIZE deliberately: this stage pays a fixed
# per-batch prompt overhead (rules + JSON schema instructions), and Groq's
# 200,000-token/day free allowance forces amortizing that over more offers,
# not fewer. Blunt whole-description truncation (formerly 5000 chars) at
# post-reshape volume (~517 new offers/day across 4 tiers) cost ~510,000
# tokens/day - 2.5x the allowance - and dies partway through the first tier,
# falling every later offer back to "unconfirmed". See _extract_policy_excerpt.
BATCH_SIZE = 8
# Per-offer excerpt budget AND the fallback prefix length for a description
# with no detected remote/location keyword (see _POLICY_KEYWORDS) - kept small
# because a passage that never names a work-location term is, by
# _build_prompt's own instructions, going to yield "unconfirmed" regardless of
# how much of it the model sees. Changing it moves the whole stage's daily
# Groq token spend, not just the fallback.
_MAX_DESC_CHARS = 1000
_MAX_RETRIES = 4
_GROQ_MODEL = "openai/gpt-oss-20b"

# Groq's account-wide free-tier daily token budget for this stage's model
# (confirmed via the 429 body: "tokens per day (TPD): Limit 200000"). Owned
# here rather than in main.py because the proactive failover check below
# needs it; main.py imports it rather than keeping its own copy so the two
# never drift apart.
# Sourced from config/llm_limits.json; the literal is only the safety fallback
# when that file is missing or the entry is unknown, so a config problem can
# never leave the stage without a budget.
_GROQ_DAILY_TOKEN_LIMIT_FALLBACK = 200_000
_groq_limit = limit_for("groq", _GROQ_MODEL)
GROQ_DAILY_TOKEN_LIMIT = (_groq_limit.per_day if _groq_limit and _groq_limit.per_day
                          else _GROQ_DAILY_TOKEN_LIMIT_FALLBACK)
# Failing over BEFORE the cap is hit (main.py already tracks the day's
# running Groq total and passes it in as groq_tokens_used_today) needs a
# safety margin at least as large as one worst-case batch, so a proactive
# switch can't itself be the call that goes over. Measured worst case for
# BATCH_SIZE=8 is ~5,500 tokens; kept a bit above that.
_GROQ_TPD_HEADROOM_TOKENS = 6_000
# Free-plan ceiling is 8,000 tokens/minute and one 8-offer batch costs
# ~4,500-5,500 tokens (Groq rate-limits page, fetched 2026-09-09), so two
# back-to-back batches reliably trip TPM even though neither is anywhere near
# the daily cap. A pause between Groq batches removes most (not all - two
# batches can still land in the same rolling 60s window depending on how long
# each call itself takes) of those retries at a real but bounded cost: at
# ~20-30 batches/tier this adds roughly 8-12 minutes to a tier's run. Worth
# it against a ladder that otherwise burns ~35s of retries per throttled
# batch and, unlike the TPD case, would previously repeat every batch.
_GROQ_BATCH_PAUSE_SECONDS = 25
# A run where every batch failed was already flagged; a run where 27 of 28
# did (2026-09-08 tier 1, attempt 3) was not, and read as healthy. Set well
# below that (96%) so a real multi-batch outage or exhausted-with-no-fallback
# run is caught, and well above the odds of one random transient batch in an
# otherwise-healthy ~20-30 batch tier.
_DEGRADED_FAILURE_RATIO = 0.1

# The remote/hybrid/on-site policy sentence is not reliably near the top of a
# posting - measured against real 2026-09-08 production descriptions, over
# half the offers that mention a work-location term first mention it past
# char 1000, and some real postings ("...This role is based onsite in our
# <city> office...") state it only past char 7000, well beyond even the
# previous 5000-char truncation. A flat character cutoff either burns budget
# on irrelevant prose or silently drops the one sentence the verdict hinges
# on. _extract_policy_excerpt anchors on the keyword itself instead, so the
# input is small AND the signal survives wherever it falls in the posting.
_POLICY_KEYWORDS = (
    "remote", "remoto", "hybrid", "ibrid", "on-site", "onsite", "in sede",
    "in ufficio", "office", "presenza", "presenziale", "smart working",
    "full-remote", "full remote", "work from", "wfh", "sede di lavoro",
    "modalità di lavoro", "in person", "trasferta", "in loco",
)
# Chars of context kept on each side of a detected keyword, and always kept
# from the very start of the description (job-intro context, e.g. a
# one-line "fully remote across the EU" summary before the full text).
_EXCERPT_WINDOW = 180
_EXCERPT_INTRO_CHARS = 200
_EXCERPT_SEPARATOR = " [...] "

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Fallback pins are perishable (see data/learnings.md 2026-09-06). Verified
# listed on OpenRouter's public /api/v1/models, with tool-calling support
# ("tools" in supported_parameters, required by the function-calling
# structured output below) on 2026-09-29:
# - nvidia/nemotron-3-super-120b-a12b:free: same model already proven in
#   production for scoring's structurally similar judgement task
#   (src/scorer.py's _OPENROUTER_MODEL). A 2026-09-29 spot check re-verified
#   42 Groq-judged production offers through this path only: 57% raw
#   agreement, and materially more conservative than Groq (only 8 of 21
#   Groq-confirmed stayed confirmed, most dropping to unconfirmed on an
#   ambiguous "remote-first"/"100% remote" phrase). The dangerous direction
#   (wrongly confirming a Groq-rejected offer) could not be measured, since
#   rejected offers are never persisted to Neon; that needs a labeled golden
#   set covering all three verdict classes.
# - google/gemma-4-26b-a4b-it:free: same fallback already proven in
#   production via the scorer.
# - thinkingmachines/inkling-small:free: a third provider (neither nvidia nor
#   google), so a single-provider OpenRouter outage still leaves one more
#   entry to fall back to - the same reasoning as src/scorer.py's "one per
#   provider" comment. Deliberately NOT poolside/laguna-s-2.1:free, which
#   OpenRouter's own listing describes as a coding-agent model, a worse fit
#   for a work-location judgement call.
# Kept independent of src/scorer.py's own array (rather than importing it):
# that array is a known-stale pin (z-ai/glm-5.2:free is delisted - a separate
# tracked issue, out of scope here) and this stage's correctness must not
# inherit scoring's drift.
_OPENROUTER_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"
# OpenRouter rejects extra_body["models"] above 3 entries total (primary +
# this list) - see src/scorer.py's _OPENROUTER_FALLBACK_MODELS comment for
# the 2026-09-01 outage that taught that lesson.
_OPENROUTER_FALLBACK_MODELS = [
    "google/gemma-4-26b-a4b-it:free",
    "thinkingmachines/inkling-small:free",
]
# OpenRouter's $0 tier caps the whole account at 50 requests per UTC day, and
# scoring - which runs after this stage in every tier - spends that same
# budget. The failover may use at most this many of them per UTC day
# (retries included), so a Groq-exhausted day still leaves scoring its half
# instead of verification starving it. main.py passes the day's running
# count in as openrouter_requests_used_today.
OPENROUTER_VERIFICATION_DAILY_REQUEST_CAP = 25


def _keyword_anchors(lower: str) -> list[tuple[int, int]]:
    anchors = []
    for keyword in _POLICY_KEYWORDS:
        start = 0
        while True:
            idx = lower.find(keyword, start)
            if idx == -1:
                break
            anchors.append((idx, idx + len(keyword)))
            start = idx + len(keyword)
    return sorted(anchors)


def _merge_anchors(anchors: list[tuple[int, int]], radius: int, length: int) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in anchors:
        span = (max(0, start - radius), min(length, end + radius))
        if merged and span[0] <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], span[1]))
        else:
            merged.append(span)
    return merged


def _join_spans(description: str, spans: list[tuple[int, int]]) -> str:
    return _EXCERPT_SEPARATOR.join(description[start:end] for start, end in spans)


def _extract_policy_excerpt(description: str, budget: int = _MAX_DESC_CHARS) -> str:
    """Return the passages of `description` likely to state its work-location
    policy, capped at `budget` chars including the joiners. Falls back to a flat
    prefix when no policy keyword is found - see the _MAX_DESC_CHARS comment for
    why that's safe.

    When the windows do not all fit, the context radius shrinks uniformly rather
    than the excerpt being filled front-to-back: a decisive on-site sentence at
    the end of a posting must not be crowded out by earlier remote-flavoured
    boilerplate, which is the failure a flat prefix already had.
    """
    lower = description.lower()
    anchors = _keyword_anchors(lower)
    if not anchors:
        return description[:budget]

    radius = _EXCERPT_WINDOW
    while radius > 0:
        spans = _merge_anchors(anchors, radius, len(description))
        if spans[0][0] > 0:
            spans.insert(0, (0, min(_EXCERPT_INTRO_CHARS, spans[0][0])))
        text = _join_spans(description, spans)
        if len(text) <= budget:
            return text
        radius //= 2

    # Pathologically keyword-dense text: keep an evenly strided subset of the
    # bare keyword hits (first and last always among them) so the survivors
    # still span the whole posting.
    spans = _merge_anchors(anchors, 0, len(description))
    for step in range(1, len(spans) + 1):
        selected = spans[::step]
        if selected[-1] != spans[-1]:
            selected.append(spans[-1])
        text = _join_spans(description, selected)
        if len(text) <= budget:
            return text
    return _join_spans(description, [spans[-1]])[:budget]


_NO_DESCRIPTION_REASON = "Description unavailable, could not verify."
_DEGRADED_REASON = "Verification unavailable, treated as unconfirmed."

_ITALY_RULE = (
    "confirmed ONLY when the description shows the role is fully remote AND that "
    "someone living in Italy can hold it. An explicit mention of Italy, of the EU "
    "or Europe generally, or of remote work with no country restriction all "
    "count. rejected when the role needs any on-site or hybrid presence, or when "
    "it restricts residency to a country that is not Italy."
)

_REMOTE_ONLY_RULE = (
    "confirmed ONLY when the description shows the role is fully remote, with no "
    "required days in an office. rejected when the role needs any on-site or "
    "hybrid presence."
)


class _VerdictItem(BaseModel):
    id: int
    verdict: str
    reason: str = ""

    @field_validator("verdict")
    @classmethod
    def _canonical(cls, value: str) -> str:
        # The model is asked for lowercase but drifts to "Confirmed"; everything
        # downstream matches on the canonical lowercase form, so fold it here at
        # the boundary rather than at each comparison.
        return value.strip().lower()


class _VerdictOutput(BaseModel):
    offers: list[_VerdictItem]


class _OpenRouterShareSpent(Exception):
    """This stage's daily share of OpenRouter requests is used up."""


def _client(api_key: str):
    from groq import Groq

    # max_retries=0: _verify_batch's own ladder is the only retry layer, so a
    # TPD 429 is seen on the first hit instead of after the SDK's retries.
    return Groq(api_key=api_key, max_retries=0)


def _is_daily_quota_exceeded(e: Exception) -> bool:
    """Tell Groq's real daily-token-budget exhaustion apart from a transient
    per-minute throttle (RPM/TPM). Groq's 429 body for the daily case
    literally contains "tokens per day (TPD)"; a TPM/RPM throttle does not.
    Mirrors the role of src/scorer.py's _is_quota_exceeded (detect the
    unrecoverable-today case so the caller stops instead of retrying it
    away) - the signal itself has to differ because Groq's error body carries
    an explicit phrase where OpenRouter's only carries a reset timestamp, so
    the two functions inspect different fields, but the batch-level retry
    helpers below (_is_quota_exceeded, _is_retryable_upstream_value_error,
    _EmptyStructuredOutput) ARE imported and reused as-is once this stage
    talks to OpenRouter, rather than re-implemented.
    """
    body = str(getattr(e, "body", "") or "")
    message = str(getattr(e, "message", "") or "")
    return "tokens per day" in (body + " " + message).lower()


def _build_prompt(batch: list[JobOffer], require_italy_eligibility: bool) -> str:
    rule = _ITALY_RULE if require_italy_eligibility else _REMOTE_ONLY_RULE
    offers_text = "\n\n".join(
        f"ID: {o.id}\nTitle: {o.title}\nCompany: {o.company}\n"
        f"Location: {o.location}\nDescription: {_extract_policy_excerpt(o.description)}"
        for o in batch
    )
    return (
        "You check whether job offers are genuinely full-remote. For each offer "
        "return exactly one verdict.\n\n"
        f"Rules: {rule}\n"
        "unconfirmed when the description simply does not settle the question. "
        "Never guess: if the text is silent or vague, answer unconfirmed rather "
        "than confirmed or rejected.\n\n"
        "The reason must be one sentence naming the phrase in the description "
        "that drove your verdict.\n\n"
        f"Offers:\n{offers_text}\n\n"
        f"Return ONLY a JSON object, no prose and no markdown fences, with an "
        f"\"offers\" array holding exactly {len(batch)} objects, each with keys "
        "\"id\" (the same id you were given), \"verdict\" (one of \"confirmed\", "
        "\"rejected\", \"unconfirmed\") and \"reason\" (one sentence)."
    )


@functools.lru_cache(maxsize=2)
def _prompt_version(require_italy_eligibility: bool) -> str:
    try:
        rule = _ITALY_RULE if require_italy_eligibility else _REMOTE_ONLY_RULE
        return telemetry.prompt_version(inspect.getsource(_build_prompt), rule)
    except Exception:
        return "unknown"


def _verify_batch(client, batch: list[JobOffer], require_italy_eligibility: bool) -> tuple[dict, dict]:
    """Return (verdicts by offer id, token usage). Raises when every retry fails."""
    prompt = _build_prompt(batch, require_italy_eligibility)
    last_error = None
    for attempt in range(_MAX_RETRIES):
        try:
            with telemetry.llm_call(stage="verification", provider="groq",
                                    request_model=_GROQ_MODEL, batch_size=len(batch),
                                    attempt=attempt + 1,
                                    prompt_version=_prompt_version(require_italy_eligibility),
                                    is_quota_exhausted=_is_daily_quota_exceeded) as call:
                response = client.chat.completions.create(
                    model=_GROQ_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"},
                    temperature=0.0,
                )
                call.set_usage(response_model=getattr(response, "model", None),
                               input_tokens=getattr(response.usage, "prompt_tokens", None),
                               output_tokens=getattr(response.usage, "completion_tokens", None))
                parsed = _VerdictOutput.model_validate_json(response.choices[0].message.content)
            usage = {
                "prompt_tokens": getattr(response.usage, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(response.usage, "completion_tokens", 0) or 0,
                "total_tokens": getattr(response.usage, "total_tokens", 0) or 0,
            }
            verdicts = {
                item.id: item
                for item in parsed.offers
                if item.verdict in ("confirmed", "rejected", "unconfirmed")
            }
            return verdicts, usage
        except (ValidationError, json.JSONDecodeError) as e:
            # A malformed body is worth one more try at temperature 0, but it is
            # not an outage - do not spend the full ladder on it.
            last_error = e
            if attempt >= 1:
                raise
            time.sleep(2)
        except groq.RateLimitError as e:
            if _is_daily_quota_exceeded(e):
                # Genuinely exhausted for the day - resets at the next UTC day
                # boundary, so no amount of retrying here recovers it. Raise
                # immediately (no sleep) instead of burning the ladder, and let
                # verify_offers decide whether to fail over to OpenRouter.
                raise
            last_error = e
            if attempt == _MAX_RETRIES - 1:
                raise
            wait = min(5 * (2 ** attempt), 60)
            print(f"[verifier] Rate limited, retrying in {wait}s "
                  f"(attempt {attempt + 1}/{_MAX_RETRIES})...")
            time.sleep(wait)
        except Exception as e:
            last_error = e
            if attempt == _MAX_RETRIES - 1:
                raise
            wait = min(5 * (2 ** attempt), 60)
            print(f"[verifier] {type(e).__name__}, retrying in {wait}s "
                  f"(attempt {attempt + 1}/{_MAX_RETRIES})...")
            time.sleep(wait)
    raise last_error


def _openrouter_chain(llm_api_key: str):
    llm = ChatOpenAI(
        model=_OPENROUTER_MODEL,
        api_key=llm_api_key,
        base_url=_OPENROUTER_BASE_URL,
        extra_body={"models": _OPENROUTER_FALLBACK_MODELS},
        temperature=0.0,
        # _verify_batch_openrouter's own ladder is the only retry layer, so
        # every HTTP request is one counted against this stage's share.
        max_retries=0,
    )
    return (
        ChatPromptTemplate.from_messages([("human", "{prompt}")])
        # function_calling (not the default json_schema/response_format
        # method) because that's the only mode src/scorer.py validated clean
        # (no reasoning-token bloat) across these exact free models.
        | llm.with_structured_output(_VerdictOutput, method="function_calling")
    )


def _verify_batch_openrouter(
    chain, batch: list[JobOffer], require_italy_eligibility: bool,
    usage: dict, requests_allowed: int,
) -> dict:
    """OpenRouter fallback for a Groq-exhausted stage. Reuses src/scorer.py's
    failure classifiers (_is_quota_exceeded, _is_retryable_upstream_value_error,
    _EmptyStructuredOutput) rather than a second, divergent set: this call
    hits the same provider with the same documented failure shapes
    src/scorer.py already has proven detectors for.

    Every attempt, retries included, is counted in usage["openrouter_requests"]
    and raises _OpenRouterShareSpent instead of going past requests_allowed.
    Returns the verdicts by offer id and adds token usage into `usage`.
    """
    prompt = _build_prompt(batch, require_italy_eligibility)
    counter = _TokenCounter()
    last_error = None
    for attempt in range(_MAX_RETRIES):
        if usage["openrouter_requests"] >= requests_allowed:
            raise _OpenRouterShareSpent()
        usage["openrouter_requests"] += 1
        try:
            counter.begin_call()
            with telemetry.llm_call(stage="verification", provider="openrouter",
                                    request_model=_OPENROUTER_MODEL, batch_size=len(batch),
                                    attempt=attempt + 1,
                                    prompt_version=_prompt_version(require_italy_eligibility),
                                    is_quota_exhausted=_openrouter_quota_exhausted) as call:
                result = chain.invoke({"prompt": prompt}, config={"callbacks": [counter]})
                call.set_usage(response_model=counter.last_model,
                               input_tokens=counter.last_prompt_tokens,
                               output_tokens=counter.last_completion_tokens)
                if result is None:
                    raise _EmptyStructuredOutput()
            verdicts = {
                item.id: item
                for item in result.offers
                if item.verdict in ("confirmed", "rejected", "unconfirmed")
            }
            usage["openrouter_prompt_tokens"] += counter.prompt_tokens
            usage["openrouter_completion_tokens"] += counter.completion_tokens
            usage["openrouter_total_tokens"] += counter.total_tokens
            return verdicts
        except _EmptyStructuredOutput as e:
            last_error = e
            if attempt == _MAX_RETRIES - 1:
                raise
            wait = min(5 * (2 ** attempt), 60)
            print(f"[verifier] OpenRouter returned no structured output, retrying in {wait}s "
                  f"(attempt {attempt + 1}/{_MAX_RETRIES})...")
            time.sleep(wait)
        except openai.RateLimitError as e:
            if _is_quota_exceeded(e):
                raise  # OpenRouter's own daily cap - no point retrying, propagate
            last_error = e
            if attempt == _MAX_RETRIES - 1:
                raise
            wait = min(5 * (2 ** attempt), 60)
            print(f"[verifier] OpenRouter rate limited, retrying in {wait}s "
                  f"(attempt {attempt + 1}/{_MAX_RETRIES})...")
            time.sleep(wait)
        except (openai.InternalServerError, openai.APIConnectionError) as e:
            last_error = e
            if attempt == _MAX_RETRIES - 1:
                raise
            wait = min(5 * (2 ** attempt), 60)
            print(f"[verifier] OpenRouter upstream error ({type(e).__name__}), retrying in {wait}s "
                  f"(attempt {attempt + 1}/{_MAX_RETRIES})...")
            time.sleep(wait)
        except ValueError as e:
            if not _is_retryable_upstream_value_error(e):
                raise
            last_error = e
            if attempt == _MAX_RETRIES - 1:
                raise
            wait = min(5 * (2 ** attempt), 60)
            print(f"[verifier] OpenRouter upstream error (5xx via 200 body), retrying in {wait}s "
                  f"(attempt {attempt + 1}/{_MAX_RETRIES})...")
            time.sleep(wait)
    raise last_error


def verify_offers(
    offers: list[JobOffer],
    require_italy_eligibility: bool,
    groq_api_key: str,
    llm_api_key: str = "",
    groq_tokens_used_today: int = 0,
    openrouter_requests_used_today: int = 0,
) -> tuple[list[JobOffer], dict]:
    """Mark each offer confirmed, rejected or unconfirmed. Never raises.

    llm_api_key (OpenRouter) is optional: without it, the stage keeps its
    pre-failover behaviour of marking a Groq-unavailable run's offers
    unconfirmed, just without wasting retries on a dead quota.
    groq_tokens_used_today lets the caller (main.py, which already tracks the
    day's running Groq total across tiers) make this run fail over BEFORE it
    contributes to blowing the cap, not only after a 429 proves it already
    has. openrouter_requests_used_today does the same for this stage's
    share of OpenRouter's daily request cap
    (OPENROUTER_VERIFICATION_DAILY_REQUEST_CAP).
    """
    usage = {
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        "openrouter_prompt_tokens": 0, "openrouter_completion_tokens": 0,
        "openrouter_total_tokens": 0, "openrouter_requests": 0,
        "degraded": False, "provider": "none",
        "failed_batches": 0, "total_batches": 0,
    }
    if not offers:
        return [], usage

    checkable = []
    for offer in offers:
        description = offer.description.strip()
        # The scraper falls back to "<title> at <company>" when a job page is
        # unreadable. That carries no remote signal, so judging it would risk a
        # rejected verdict drawn from the location line alone.
        content_free = description == f"{offer.title} at {offer.company}"
        if not description or offer.description_status == "failed" or content_free:
            offer.remote_verdict = "unconfirmed"
            offer.remote_reason = _NO_DESCRIPTION_REASON
        else:
            checkable.append(offer)

    # Groq being unusable from the start (no key, or the client fails to
    # build) is folded into the SAME groq_exhausted flag the daily-budget
    # cases use below, rather than an early return, so it goes through the
    # same OpenRouter-fallback path instead of a second, more limited
    # "unconfirmed and give up" branch. Without this, a missing/broken Groq
    # key skipped OpenRouter even when it was configured and healthy.
    client = None
    groq_exhausted = False
    if not groq_api_key:
        print("[verifier] No Groq API key" +
              (" - failing over to OpenRouter." if llm_api_key else
               " - marking every offer unconfirmed."))
        groq_exhausted = True
    else:
        try:
            client = _client(groq_api_key)
        except Exception as e:
            print(f"[verifier] Could not build the Groq client ({type(e).__name__}: {e})" +
                  (" - failing over to OpenRouter." if llm_api_key else
                   " - marking every offer unconfirmed."))
            groq_exhausted = True

    openrouter_chain = _openrouter_chain(llm_api_key) if llm_api_key else None
    openrouter_exhausted = False
    openrouter_requests_allowed = (
        OPENROUTER_VERIFICATION_DAILY_REQUEST_CAP - openrouter_requests_used_today
    )

    total_batches = (len(checkable) - 1) // BATCH_SIZE + 1 if checkable else 0
    failed_batches = 0
    used_groq = False

    for i in range(0, len(checkable), BATCH_SIZE):
        batch = checkable[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1

        if not groq_exhausted:
            groq_tokens_so_far = groq_tokens_used_today + usage["total_tokens"]
            if groq_tokens_so_far >= GROQ_DAILY_TOKEN_LIMIT - _GROQ_TPD_HEADROOM_TOKENS:
                print(f"[verifier] Groq verification budget is within "
                      f"{_GROQ_TPD_HEADROOM_TOKENS} tokens of the {GROQ_DAILY_TOKEN_LIMIT} "
                      f"daily cap ({groq_tokens_so_far} used today) - failing over to "
                      f"OpenRouter before batch {batch_num}/{total_batches} runs.")
                groq_exhausted = True

        verdicts = None
        if not groq_exhausted:
            if used_groq:
                time.sleep(_GROQ_BATCH_PAUSE_SECONDS)
            print(f"[verifier] Verifying batch {batch_num}/{total_batches} "
                  f"({len(batch)} offers) via Groq...")
            used_groq = True
            try:
                verdicts, batch_usage = _verify_batch(client, batch, require_italy_eligibility)
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    usage[key] += batch_usage[key]
            except groq.RateLimitError as e:
                if _is_daily_quota_exceeded(e):
                    print(f"[verifier] Groq daily token budget exhausted at batch "
                          f"{batch_num}/{total_batches} - stopping Groq calls for the "
                          f"rest of this run (resets at the next UTC day).")
                    groq_exhausted = True
                    verdicts = None
                else:
                    print(f"[verifier] Batch {batch_num}/{total_batches} failed "
                          f"({type(e).__name__}: {e}) - marking it unconfirmed and continuing.")
                    verdicts = {}
                    failed_batches += 1
            except Exception as e:
                print(f"[verifier] Batch {batch_num}/{total_batches} failed "
                      f"({type(e).__name__}: {e}) - marking it unconfirmed and continuing.")
                verdicts = {}
                failed_batches += 1

        if verdicts is None:
            if openrouter_chain is not None and not openrouter_exhausted:
                print(f"[verifier] Verifying batch {batch_num}/{total_batches} "
                      f"({len(batch)} offers) via OpenRouter (Groq unavailable)...")
                try:
                    verdicts = _verify_batch_openrouter(
                        openrouter_chain, batch, require_italy_eligibility,
                        usage, openrouter_requests_allowed,
                    )
                except _OpenRouterShareSpent:
                    print(f"[verifier] Verification's daily share of OpenRouter requests "
                          f"({OPENROUTER_VERIFICATION_DAILY_REQUEST_CAP}) is spent at batch "
                          f"{batch_num}/{total_batches} - stopping OpenRouter calls so "
                          f"scoring keeps the rest of the daily cap.")
                    openrouter_exhausted = True
                    verdicts = {}
                    failed_batches += 1
                except openai.RateLimitError as e:
                    if _is_quota_exceeded(e):
                        # OpenRouter's own account-wide daily cap - shared with
                        # scoring later in this same tier's run, so stop
                        # spending it here too rather than repeating this same
                        # doomed call for every remaining batch.
                        print(f"[verifier] OpenRouter's own daily quota exhausted at "
                              f"batch {batch_num}/{total_batches} - stopping OpenRouter "
                              f"calls for the rest of this run too.")
                        openrouter_exhausted = True
                    else:
                        print(f"[verifier] OpenRouter batch {batch_num}/{total_batches} failed "
                              f"({type(e).__name__}: {e}) - marking it unconfirmed and continuing.")
                    verdicts = {}
                    failed_batches += 1
                except Exception as e:
                    print(f"[verifier] OpenRouter batch {batch_num}/{total_batches} failed "
                          f"({type(e).__name__}: {e}) - marking it unconfirmed and continuing.")
                    verdicts = {}
                    failed_batches += 1
            else:
                verdicts = {}
                failed_batches += 1

        for offer in batch:
            item = verdicts.get(offer.id)
            if item is None:
                offer.remote_verdict = "unconfirmed"
                offer.remote_reason = _DEGRADED_REASON
            else:
                offer.remote_verdict = item.verdict
                offer.remote_reason = item.reason

    used_openrouter = usage["openrouter_requests"] > 0
    if used_openrouter and used_groq:
        usage["provider"] = "groq+openrouter"
    elif used_openrouter:
        usage["provider"] = "openrouter"
    elif used_groq:
        usage["provider"] = "groq"
    else:
        usage["provider"] = "none"

    # Degraded means a material share of what this stage was asked to judge
    # came back from a stage failure, not a real verdict - not only the
    # all-batches-failed case, which let a 27-of-28 failure (2026-09-08 tier
    # 1, attempt 3) report itself healthy. Offers skipped for a missing
    # description are not a failure, so they do not count toward this.
    usage["degraded"] = (
        total_batches > 0 and failed_batches / total_batches >= _DEGRADED_FAILURE_RATIO
    )
    usage["failed_batches"] = failed_batches
    usage["total_batches"] = total_batches
    return offers, usage
