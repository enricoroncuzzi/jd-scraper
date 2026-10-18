"""Stage 3: the remote check (spec sections 5 and 6.2).

Deterministic rules settle what they can without a model call: a tier that skips
the check (tier 2), the source's own hybrid/on-site work mode (switched on,
99.8% agreement on 420 decided jobs), and the phrase rules in src.remote_rules.py
(confident hybrid/on-site, confident fully-remote, eligibility advice, and a
description with no work-mode vocabulary at all). Everything else is verified in
batches by the existing src.remote_verifier.verify_offers. A verdict that is only
a stage failure ("Verification unavailable") is NOT stored: the job waits and
retries.

The verdict vocabulary lives in src/remote_verdicts.py: `advised` is delivered
with a warning (D3), `not_stated` is digest-only and never alerts (D1).
"""
from datetime import datetime

from src.jobstore import budget
from src.processor.config import ProcessorConfig
from src.processor.llm_stages import (
    LlmKeys, budget_open, due_batch, retry_delay, to_offers, waiting_rows,
)
from src.processor.llm_sink import batch_context
from src.processor.state import defer, set_stage
from src.remote_rules import RULE_PREFIX, classify
from src.remote_verdicts import REJECTED
from src.remote_verifier import _DEGRADED_REASON, verify_offers

STRUCTURED_REASON = "Source states the work mode."


def structured_verdict(remote_hint: str | None) -> tuple[str, str] | None:
    """A verdict from the source's own work-mode field, or None to ask the model.

    Only hybrid/on-site is a shortcut: rejected is safe in every tier that
    requires remote work, and the measured agreement is 99.8% on 420 decided
    jobs. The confirm cases ("remote_worldwide" 87.3%, "remote" in Italy 66.7%)
    are below the project's own 95% bar, so they always go to the model.
    """
    if remote_hint in ("hybrid", "onsite"):
        return REJECTED, f"{STRUCTURED_REASON} ({remote_hint})"
    return None


def apply_deterministic_rules(conn, cfg: ProcessorConfig, now: datetime) -> int:
    """Settle verdicts that need no model call. Returns how many jobs were settled."""
    settled = 0
    with conn.cursor() as cur:
        rows = waiting_rows(cur, "remote_verdict IS NULL", (), now)
        for row in rows:
            policy = cfg.policies[row.tier]
            verdict: tuple[str, str, str] | None = None
            reason_code = "remote_rejected_structured"
            if not policy.remote_check_enabled:
                verdict = ("not_checked", "", "")
            else:
                if cfg.structured_remote_enabled:
                    structured = structured_verdict(row.remote_hint)
                    if structured is not None:
                        verdict = (structured[0], structured[1], "")
                if verdict is None and cfg.remote_rules_enabled:
                    ruled = classify(
                        row.description, title=row.title, location=row.location_raw,
                        remote_hint=row.remote_hint,
                        require_italy_eligibility=policy.require_italy_eligibility,
                        # The source-hint shortcut is decided above, by its own switch.
                        hints_enabled=False)
                    if ruled is not None:
                        verdict = (ruled.verdict, ruled.reason, ruled.advice)
                        if ruled.reason.startswith(RULE_PREFIX):
                            reason_code = "remote_rejected_rule"
            if verdict is None:
                continue
            cur.execute(
                "UPDATE jobs SET remote_verdict = %s, remote_reason = %s, remote_advice = %s "
                "WHERE id = %s", (*verdict, row.id))
            if verdict[0] == REJECTED:
                set_stage(cur, row.id, "filtered_out", reason_code, now)
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
                cur.execute(
                    "UPDATE jobs SET remote_verdict = %s, remote_reason = %s, remote_advice = %s "
                    "WHERE id = %s",
                    (offer.remote_verdict, offer.remote_reason, offer.remote_advice, row.id))
                if offer.remote_verdict == REJECTED:
                    set_stage(cur, row.id, "filtered_out", "remote_rejected", now)
                stored += 1
        conn.commit()
    return stored
