"""Stage 3: the remote check (spec sections 5 and 6.2).

Deterministic rules settle what they can without a model call: a tier that skips
the check (tier 2), and, only when switched on, a source's own structured
work-mode. Everything else is verified in batches by the existing
src.remote_verifier.verify_offers, unchanged. A verdict that is only a stage
failure ("Verification unavailable") is NOT stored: the job waits and retries.
"""
from datetime import datetime

from src.jobstore import budget
from src.processor.config import ProcessorConfig, TierPolicy
from src.processor.llm_stages import (
    LlmKeys, budget_open, due_batch, retry_delay, to_offers, waiting_rows,
)
from src.processor.llm_sink import batch_context
from src.processor.state import defer, set_stage
from src.remote_verifier import _DEGRADED_REASON, verify_offers

STRUCTURED_REASON = "Source states the work mode."


def structured_verdict(remote_hint: str | None, country: str | None,
                       policy: TierPolicy) -> tuple[str, str] | None:
    """A verdict from the source's own work-mode field, or None to ask the model.

    Rejected is safe in every tier that requires remote work. Confirmed is safe
    for a role open worldwide, and for a remote role located in Italy where the rule
    is "fully remote" with no residency condition (tier 1): a remote role in
    Germany says nothing about whether Italy may apply.
    """
    if remote_hint in ("hybrid", "onsite"):
        return "rejected", f"{STRUCTURED_REASON} ({remote_hint})"
    if remote_hint == "remote_worldwide":
        # Remote with no country restriction satisfies both rules, Italy's included.
        return "confirmed", f"{STRUCTURED_REASON} (remote, open worldwide)"
    if remote_hint == "remote" and not policy.require_italy_eligibility and country == "italy":
        return "confirmed", f"{STRUCTURED_REASON} (remote, located in Italy)"
    return None


def apply_deterministic_rules(conn, cfg: ProcessorConfig, now: datetime) -> int:
    """Settle verdicts that need no model call. Returns how many jobs were settled."""
    settled = 0
    with conn.cursor() as cur:
        rows = waiting_rows(cur, "remote_verdict IS NULL", (), now)
        for row in rows:
            policy = cfg.policies[row.tier]
            if not policy.remote_check_enabled:
                verdict = ("not_checked", "")
            elif cfg.structured_remote_enabled:
                verdict = structured_verdict(row.remote_hint, row.country, policy)
            else:
                verdict = None
            if verdict is None:
                continue
            cur.execute("UPDATE jobs SET remote_verdict = %s, remote_reason = %s WHERE id = %s",
                        (verdict[0], verdict[1], row.id))
            if verdict[0] == "rejected":
                set_stage(cur, row.id, "filtered_out", "remote_rejected_structured", now)
            settled += 1
    conn.commit()
    return settled


def run_remote_batches(conn, cfg: ProcessorConfig, now: datetime, keys: LlmKeys, *,
                       verify=verify_offers, max_batches: int = 3) -> int:
    """Verify up to max_batches due batches. Returns how many jobs got a stored verdict."""
    stored = 0
    for _ in range(max_batches):
        if not budget_open(conn, cfg, now):
            break
        batch = None
        for require_italy in (False, True):
            tiers = [t for t, p in sorted(cfg.policies.items())
                     if p.remote_check_enabled and p.require_italy_eligibility == require_italy]
            with conn.cursor() as cur:
                rows = waiting_rows(cur, "remote_verdict IS NULL AND tier = ANY(%s)", (tiers,), now)
            batch = due_batch(rows, cfg.remote_check_batch_size, cfg.flush_after_minutes, now)
            if batch:
                break
        if not batch:
            break
        groq_used = budget.used_today(conn, "groq", now)
        with batch_context(row.id for row in batch):
            verified, _usage = verify(
                offers=to_offers(batch), require_italy_eligibility=require_italy,
                groq_api_key=keys.groq_api_key, llm_api_key=keys.llm_api_key,
                groq_tokens_used_today=groq_used.input_tokens + groq_used.output_tokens,
                openrouter_requests_used_today=budget.stage_requests_today(
                    conn, "openrouter", "verification", now),
                batch_size=cfg.remote_check_batch_size)
        with conn.cursor() as cur:
            for row, offer in zip(batch, verified):
                if offer.remote_reason == _DEGRADED_REASON:
                    defer(cur, row.id, now, retry_delay(row.attempts))
                    continue
                cur.execute("UPDATE jobs SET remote_verdict = %s, remote_reason = %s WHERE id = %s",
                            (offer.remote_verdict, offer.remote_reason, row.id))
                if offer.remote_verdict == "rejected":
                    set_stage(cur, row.id, "filtered_out", "remote_rejected", now)
                stored += 1
        conn.commit()
    return stored
