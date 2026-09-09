"""Deferred-offer retry queue: the offers a run fetched but never scored.

Scoring can stop partway through a tier - OpenRouter's daily free-tier cap,
an upstream 5xx that outlives the batch retry ladder, a model that keeps
skipping the forced tool call. `src/scorer.py`'s `score_offers` returns what it
scored and stops there, which is the right behaviour for the scorer but used to
be fatal one level up: `main.py` marked EVERY new offer seen, so the unscored
remainder was absent from the digest, the notes, Postgres and Telegram while
being recorded in the dedup log. No later run could ever see those offers
again. On 2026-09-07 tier 4 scored 20 of 242 new offers, destroyed 222 and
logged "Done." with exit code 0.

The root mismatch is what the dedup log means: it records "this offer was
fetched once", while every caller reads it as "this offer was handled". This
queue holds the difference - one JSONL file per tier, beside that tier's dedup
log (`data/seen_tier1.txt` -> `data/unscored_tier1.jsonl`, see
`AppConfig.retry_queue_path`).

Entries carry the whole `JobOffer`, description and remote verdict included:
both were already paid for (a rate-limited LinkedIn request and Groq tokens),
so the next run feeds them straight back into scoring instead of refetching or
re-verifying. They expire after `MAX_AGE_DAYS`, counted from the FIRST
deferral: a job posting goes stale, and a queue that grows without bound on a
run of bad days is its own bug.
"""

import json
import os
from datetime import datetime, timedelta

from pydantic import BaseModel, ValidationError

from src.models import JobOffer

# A posting deferred on a quota-exhausted day is still worth scoring two days
# later; by day four the role is usually closed or filled.
MAX_AGE_DAYS = 3


class QueueEntry(BaseModel):
    queued_at: datetime
    offer: JobOffer


def load_deferred(path: str, now: datetime | None = None) -> list[QueueEntry]:
    """Read the queue, dropping expired entries. A missing, empty or corrupt
    file yields nothing: the queue is a recovery aid and must never be a
    reason for a run to fail."""
    if not os.path.exists(path):
        return []
    now = now or datetime.now()
    cutoff = now - timedelta(days=MAX_AGE_DAYS)
    entries: list[QueueEntry] = []
    expired = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = QueueEntry.model_validate(json.loads(line))
            except (json.JSONDecodeError, ValidationError) as e:
                print(f"[queue] Skipping an unreadable entry in {path}: {type(e).__name__}: {e}")
                continue
            if entry.queued_at < cutoff:
                expired += 1
                continue
            entries.append(entry)
    if expired:
        print(f"[queue] Dropped {expired} deferred offer(s) older than {MAX_AGE_DAYS} day(s).")
    return entries


def build_deferred(
    offers: list[JobOffer],
    previous: list[QueueEntry],
    now: datetime | None = None,
) -> list[QueueEntry]:
    """Entries for the offers this run did not score. An offer already on the
    queue keeps its original timestamp, so expiry counts from the first
    deferral rather than being pushed back by every bad day."""
    now = now or datetime.now()
    queued_at_by_link = {entry.offer.link: entry.queued_at for entry in previous}
    return [
        QueueEntry(queued_at=queued_at_by_link.get(offer.link, now), offer=offer)
        for offer in offers
    ]


def save_deferred(path: str, entries: list[QueueEntry]) -> None:
    """Rewrite the queue with `entries`, removing the file when there are none.

    A rewrite (not an append) is what keeps expiry working: entries this run
    scored, or dropped as stale, must not survive in the file.
    """
    if not entries:
        if os.path.exists(path):
            os.remove(path)
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w") as f:
        for entry in entries:
            f.write(entry.model_dump_json() + "\n")
    os.replace(tmp_path, path)
