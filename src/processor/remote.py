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

Two source hints shape the verdict: a `remote`/`remote_worldwide` hint with a
silent description is `advised` (the source's own field is evidence), and an
explicit full-remote phrase in the title or text drops the source's hybrid/onsite
shortcut and sends the job to the model. After the model answers, a rejection
grounded only in occasional presence (both the reason and the text say so, and
neither shows a regular cadence or a work-mode "hybrid") is turned into `advised`
for a source-hinted job.
"""
from datetime import datetime

from src.jobstore import budget
from src.processor.config import ProcessorConfig
from src.processor.llm_stages import (
    LlmKeys, budget_open, due_batch, retry_delay, to_offers, waiting_rows,
)
from src.processor.llm_sink import batch_context
from src.models import JobOffer
from src.processor.state import JobRow, defer, set_stage
from src.remote_rules import (
    RULE_PREFIX, classify, confident_hybrid_phrase, confident_remote_phrase,
    occasional_presence_phrase, regular_presence_phrase, work_mode_hybrid_phrase,
)
from src.remote_verdicts import ADVISED, NOT_STATED, REJECTED
from src.remote_verifier import _DEGRADED_REASON, verify_offers

STRUCTURED_REASON = "Source states the work mode."
SOURCE_REMOTE_HINTS = ("remote", "remote_worldwide")
SOURCE_HINT_REASON = "Source marks the job remote; the text does not state the work mode."


def source_hint_advice(require_italy_eligibility: bool) -> str:
    return ("the source marks it remote - check eligibility" if require_italy_eligibility
            else "the source marks it remote")


def structured_verdict(remote_hint: str | None, *,
                       explicit_remote: str | None = None) -> tuple[str, str] | None:
    """A verdict from the source's own work-mode field, or None to ask the model.

    An explicit full-remote phrase in the title or text drops the shortcut (see
    `explicit_remote`); the model then decides, in every tier.

    Only hybrid/on-site is a shortcut: rejected is safe in every tier that
    requires remote work, and the measured agreement is 99.8% on 420 decided
    jobs. The confirm cases ("remote_worldwide" 87.3%, "remote" in Italy 66.7%)
    are below the project's own 95% bar, so they always go to the model.
    """
    if remote_hint in ("hybrid", "onsite"):
        if explicit_remote:
            return None
        return REJECTED, f"{STRUCTURED_REASON} ({remote_hint})"
    return None


def hint_remote_override(row: JobRow, offer: JobOffer, *,
                         require_italy_eligibility: bool,
                         cloud_hybrid_guard: bool = True) -> tuple[str, str, str] | None:
    """The source says remote, the model rejected, its evidence is occasional
    presence, and neither the text nor the reason shows a regular office
    pattern: the source wins.

    "Occasional-presence grounds alone" is the bar, so BOTH the model's reason
    and the text must carry an occasional-presence phrase; a reason that only
    mentions an event while describing a regular requirement does not qualify.
    The hybrid scan here is deliberately UNGUARDED: office days sharing a
    sentence with an offsite are still office days. The reason is also read for
    a regular cadence, because _CONFIDENT_HYBRID has no "twice a week" and no
    named weekdays.
    """
    if offer.remote_verdict != REJECTED or row.remote_hint not in SOURCE_REMOTE_HINTS:
        return None
    text = f"{row.title}\n{row.description}"
    if not occasional_presence_phrase(offer.remote_reason):
        return None
    if not occasional_presence_phrase(text):
        return None
    # Recall-oriented: a reason or text that names a regular cadence, or a text
    # that uses the word "hybrid" at all, keeps the model's rejection.
    if regular_presence_phrase(offer.remote_reason) or regular_presence_phrase(text):
        return None
    if work_mode_hybrid_phrase(text, cloud_hybrid_guard=cloud_hybrid_guard) is not None:
        return None
    if confident_hybrid_phrase(text, occasional_guard=False,
                               cloud_hybrid_guard=cloud_hybrid_guard) is not None:
        return None
    return (ADVISED, SOURCE_HINT_REASON, source_hint_advice(require_italy_eligibility))


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
                overrode_hint = False
                if cfg.structured_remote_enabled and row.remote_hint in ("hybrid", "onsite"):
                    explicit_remote = (confident_remote_phrase(f"{row.title}\n{row.description}")
                                       if cfg.explicit_remote_wins else None)
                    overrode_hint = bool(explicit_remote)
                    structured = structured_verdict(row.remote_hint,
                                                    explicit_remote=explicit_remote)
                    if structured is not None:
                        verdict = (structured[0], structured[1], "")
                if verdict is None and cfg.remote_rules_enabled and not overrode_hint:
                    ruled = classify(
                        row.description, title=row.title, location=row.location_raw,
                        remote_hint=row.remote_hint,
                        require_italy_eligibility=policy.require_italy_eligibility,
                        # The source-hint shortcut is decided above, by its own switch.
                        hints_enabled=False,
                        occasional_guard=cfg.occasional_presence_guard,
                        cloud_hybrid_guard=cfg.cloud_hybrid_guard)
                    if ruled is not None:
                        if (ruled.verdict == NOT_STATED and cfg.remote_hint_advice
                                and row.remote_hint in SOURCE_REMOTE_HINTS):
                            verdict = (ADVISED, SOURCE_HINT_REASON,
                                       source_hint_advice(policy.require_italy_eligibility))
                        else:
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
                batch_size=cfg.remote_check_batch_size,
                occasional_presence_prompt=cfg.occasional_presence_prompt)
        with conn.cursor() as cur:
            for row, offer in zip(batch, verified):
                if offer.remote_reason == _DEGRADED_REASON:
                    defer(cur, row.id, now, retry_delay(row.attempts))
                    continue
                verdict, reason, advice = (
                    offer.remote_verdict, offer.remote_reason, offer.remote_advice)
                if cfg.remote_hint_never_reject:
                    override = hint_remote_override(
                        row, offer,
                        require_italy_eligibility=cfg.policies[row.tier].require_italy_eligibility,
                        cloud_hybrid_guard=cfg.cloud_hybrid_guard)
                    if override is not None:
                        verdict, reason, advice = override
                cur.execute(
                    "UPDATE jobs SET remote_verdict = %s, remote_reason = %s, remote_advice = %s "
                    "WHERE id = %s", (verdict, reason, advice, row.id))
                if verdict == REJECTED:
                    set_stage(cur, row.id, "filtered_out", "remote_rejected", now)
                stored += 1
        conn.commit()
    return stored
