"""Stage 4: scoring, in batches, by the existing src.scorer.score_offers, unchanged.

Tiers whose profile and keywords are identical share batches. Whatever the scorer
did not reach (quota, 5xx) stays `described` and retries after a growing wait.
"""
from datetime import datetime

from src.processor.config import ProcessorConfig
from src.processor.llm_stages import (
    LlmKeys, budget_open, due_batch, retry_delay, to_offers, waiting_rows,
)
from src.processor.llm_sink import batch_context
from src.processor.state import defer, set_stage
from src.scorer import score_offers


def run_scoring_batches(conn, cfg: ProcessorConfig, now: datetime, keys: LlmKeys, *,
                        score=score_offers, max_batches: int = 3) -> int:
    """Score up to max_batches due batches. Returns how many jobs were scored."""
    done = 0
    groups: dict[tuple, list[int]] = {}
    for tier, policy in sorted(cfg.policies.items()):
        groups.setdefault(policy.scoring_key, []).append(tier)
    for _ in range(max_batches):
        if not budget_open(conn, cfg, now):
            break
        batch = None
        for tiers in groups.values():
            with conn.cursor() as cur:
                rows = waiting_rows(
                    cur, "remote_verdict IS NOT NULL AND remote_verdict <> 'rejected' "
                         "AND score IS NULL AND tier = ANY(%s)", (tiers,), now)
            batch = due_batch(rows, cfg.scoring_batch_size, cfg.flush_after_minutes, now)
            if batch:
                policy = cfg.policies[tiers[0]]
                break
        if not batch:
            break
        with batch_context(row.id for row in batch):
            scored, _usage = score(
                offers=to_offers(batch), profile=policy.candidate_profile,
                priority_keywords=list(policy.priority_keywords),
                exclude_keywords=list(policy.exclude_keywords), llm_api_key=keys.llm_api_key,
                batch_size=cfg.scoring_batch_size)
        by_index = {offer.id: offer for offer in scored}
        with conn.cursor() as cur:
            for index, row in enumerate(batch):
                offer = by_index.get(index)
                if offer is None:
                    defer(cur, row.id, now, retry_delay(row.attempts))
                    continue
                cur.execute("UPDATE jobs SET score = %s, comment = %s, summary = %s WHERE id = %s",
                            (offer.score, offer.comment, offer.summary, row.id))
                set_stage(cur, row.id, "scored", "", now)
                done += 1
        conn.commit()
        if len(by_index) < len(batch):
            break  # the scorer stopped early (quota or outage): do not hammer it this tick
    return done
