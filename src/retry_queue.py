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

An offer whose description LinkedIn refused (HTTP 429/503/504) is a different
entry on the same queue: `description_status` is `rate_limited`, the next run
refetches the page instead of scoring the empty text, and it is dropped after
`RATE_LIMIT_MAX_AGE_DAYS` so a throttle cannot defer it forever.
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import BaseModel, ValidationError, field_validator

from src.models import JobOffer

# A posting deferred on a quota-exhausted day is still worth scoring two days
# later; by day four the role is usually closed or filled.
MAX_AGE_DAYS = 3
# Two daily attempts (the day it first failed, and the next), then give up.
RATE_LIMIT_MAX_AGE_DAYS = 2


class QueueEntry(BaseModel):
    queued_at: datetime
    offer: JobOffer

    @field_validator("queued_at")
    @classmethod
    def _naive_local(cls, value: datetime) -> datetime:
        # This module writes naive local timestamps, but a hand-edited or
        # externally written line can carry a UTC offset, and comparing the two
        # raises TypeError in the expiry check below.
        if value.tzinfo is None:
            return value
        return value.astimezone().replace(tzinfo=None)


@dataclass
class DeferredQueue:
    live: list[QueueEntry]
    expired_rate_limited: list[QueueEntry]


def _rate_limit_expired(queued_at: datetime, now: datetime) -> bool:
    return queued_at <= now - timedelta(days=RATE_LIMIT_MAX_AGE_DAYS)


def read_deferred(path: str, now: datetime | None = None) -> DeferredQueue:
    """Live entries, plus rate-limited ones dropped for age.

    Scoring deferrals older than MAX_AGE_DAYS are logged and discarded.
    Rate-limited descriptions older than RATE_LIMIT_MAX_AGE_DAYS are returned
    separately so the caller can mark them seen: dropping them from the file
    alone would let today's scrape start a new clock. A missing, empty or
    corrupt file yields nothing. The queue is a recovery aid and must never
    be a reason for a run to fail.
    """
    if not os.path.exists(path):
        return DeferredQueue([], [])
    now = now or datetime.now()
    scoring_cutoff = now - timedelta(days=MAX_AGE_DAYS)
    live: list[QueueEntry] = []
    expired_rate_limited: list[QueueEntry] = []
    scoring_expired = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = QueueEntry.model_validate(json.loads(line))
            except (json.JSONDecodeError, ValidationError, TypeError) as e:
                print(f"[queue] Skipping an unreadable entry in {path}: {type(e).__name__}: {e}")
                continue
            if entry.offer.description_status == "rate_limited" and _rate_limit_expired(entry.queued_at, now):
                expired_rate_limited.append(entry)
                continue
            if entry.queued_at < scoring_cutoff:
                scoring_expired += 1
                continue
            live.append(entry)
    if scoring_expired:
        print(f"[queue] Dropped {scoring_expired} deferred offer(s) older than {MAX_AGE_DAYS} day(s).")
    if expired_rate_limited:
        print(f"[queue] Dropped {len(expired_rate_limited)} rate-limited offer(s) deferred "
              f"longer than {RATE_LIMIT_MAX_AGE_DAYS} day(s).")
    return DeferredQueue(live, expired_rate_limited)


def load_deferred(path: str, now: datetime | None = None) -> list[QueueEntry]:
    """Live queue entries. See read_deferred for expiry."""
    return read_deferred(path, now).live


def build_deferred(
    offers: list[JobOffer],
    previous: list[QueueEntry],
    now: datetime | None = None,
) -> list[QueueEntry]:
    """Entries for the offers this run did not score. An offer whose link is
    already on the queue keeps its original timestamp - including when today's
    scrape returned it again and it is being re-queued as a fresh copy - so
    expiry counts from the first deferral rather than being pushed back by
    every bad day. `previous` must therefore be every entry loaded from the
    queue, not just the subset carried into scoring."""
    now = now or datetime.now()
    queued_at_by_link = {entry.offer.link: entry.queued_at for entry in previous}
    entries: list[QueueEntry] = []
    for offer in offers:
        queued_at = queued_at_by_link.get(offer.link, now)
        # A rate-limited description that already had its two days must not
        # re-enter just because today's scrape saw the same link again.
        if offer.description_status == "rate_limited" and _rate_limit_expired(queued_at, now):
            continue
        entries.append(QueueEntry(queued_at=queued_at, offer=offer))
    return entries


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
