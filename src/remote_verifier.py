"""Confirm that an offer is genuinely full-remote before it costs scoring tokens.

LinkedIn's "remote" work-mode filter is not trustworthy for this: plenty of
listings are tagged remote but require residency in the posting country. This
stage reads the description itself and rules on it.

It runs on Groq rather than OpenRouter deliberately. Scoring already exhausts
the OpenRouter free-tier daily cap on bad days, and that has taken whole runs
down; putting a second stage on the same key would make that worse. Groq is a
separate key with a separate quota, already a dependency of the CV tailoring
path.

The stage is a filter, never a gate. Every failure mode - an empty description,
a batch that will not complete, a missing key - resolves to "unconfirmed", so a
bad API minute can never silently discard a real job.
"""

import json
import time

from pydantic import BaseModel, ValidationError

from src.models import JobOffer

BATCH_SIZE = 5
_MAX_DESC_CHARS = 5000
_MAX_RETRIES = 4
_GROQ_MODEL = "openai/gpt-oss-20b"

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


class _VerdictOutput(BaseModel):
    offers: list[_VerdictItem]


def _client(api_key: str):
    from groq import Groq

    return Groq(api_key=api_key)


def _build_prompt(batch: list[JobOffer], require_italy_eligibility: bool) -> str:
    rule = _ITALY_RULE if require_italy_eligibility else _REMOTE_ONLY_RULE
    offers_text = "\n\n".join(
        f"ID: {o.id}\nTitle: {o.title}\nCompany: {o.company}\n"
        f"Location: {o.location}\nDescription: {o.description[:_MAX_DESC_CHARS]}"
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


def _verify_batch(client, batch: list[JobOffer], require_italy_eligibility: bool) -> tuple[dict, dict]:
    """Return (verdicts by offer id, token usage). Raises when every retry fails."""
    prompt = _build_prompt(batch, require_italy_eligibility)
    last_error = None
    for attempt in range(_MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=_GROQ_MODEL,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.0,
            )
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
        except Exception as e:
            last_error = e
            if attempt == _MAX_RETRIES - 1:
                raise
            wait = min(5 * (2 ** attempt), 60)
            print(f"[verifier] {type(e).__name__}, retrying in {wait}s "
                  f"(attempt {attempt + 1}/{_MAX_RETRIES})...")
            time.sleep(wait)
    raise last_error


def verify_offers(
    offers: list[JobOffer],
    require_italy_eligibility: bool,
    groq_api_key: str,
) -> tuple[list[JobOffer], dict]:
    """Mark each offer confirmed, rejected or unconfirmed. Never raises."""
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "degraded": False}
    if not offers:
        return [], usage

    if not groq_api_key:
        print("[verifier] No Groq API key - marking every offer unconfirmed.")
        for offer in offers:
            offer.remote_verdict = "unconfirmed"
            offer.remote_reason = _DEGRADED_REASON
        usage["degraded"] = True
        return offers, usage

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

    try:
        client = _client(groq_api_key)
    except Exception as e:
        print(f"[verifier] Could not build the Groq client ({type(e).__name__}: {e}) "
              f"- marking every offer unconfirmed.")
        for offer in checkable:
            offer.remote_verdict = "unconfirmed"
            offer.remote_reason = _DEGRADED_REASON
        usage["degraded"] = bool(checkable)
        return offers, usage

    total_batches = (len(checkable) - 1) // BATCH_SIZE + 1 if checkable else 0
    failed_batches = 0

    for i in range(0, len(checkable), BATCH_SIZE):
        batch = checkable[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        print(f"[verifier] Verifying batch {batch_num}/{total_batches} ({len(batch)} offers)...")
        try:
            verdicts, batch_usage = _verify_batch(client, batch, require_italy_eligibility)
            for key in usage:
                if key == "degraded":
                    continue
                usage[key] += batch_usage[key]
        except Exception as e:
            print(f"[verifier] Batch {batch_num}/{total_batches} failed "
                  f"({type(e).__name__}: {e}) - marking it unconfirmed and continuing.")
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

    # Degraded means the stage could not judge anything it was asked to judge.
    # Offers skipped for a missing description are not a failure, so they do not
    # count toward this.
    usage["degraded"] = total_batches > 0 and failed_batches == total_batches
    return offers, usage
