"""The daily delivery: every scored job that has not been delivered goes into the
day's output tree in today's layout (spec section 7.1).

Crash and restart rules:
- delivery_runs (day, tier) is written in the SAME transaction that marks the jobs
  delivered, after the files are written. A restart later the same day therefore
  finds the marker and does nothing, instead of overwriting a good digest.md with
  an empty one.
- Files are written before the transaction and are idempotent, so a crash between
  the two simply redelivers the same jobs and rewrites the same files.
- A Telegram failure never undoes a delivery; telegram_sent_at stays NULL and the
  message is sent again on the next tick.
"""
import os
from dataclasses import dataclass
from datetime import date, datetime

from src.delivery.jobs import DeliveryJob, load_jobs
from src.delivery.render import note_filename, render_digest, render_note, render_rejected
from src.delivery.settings import DeliverySettings
from src.processor.config import ProcessorConfig


@dataclass
class TierDelivery:
    tier: int
    day: date
    jobs: list[DeliveryJob]
    rejected: list[DeliveryJob]
    waiting: int


def day_folder(settings: DeliverySettings, day: date, tier: int) -> str:
    return os.path.join(settings.output_path, day.isoformat(), f"tier{tier}")


def note_path(settings: DeliverySettings, job: DeliveryJob, day: date) -> str:
    return os.path.join(day_folder(settings, day, job.tier), "scraped", note_filename(job))


def write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def already_delivered(conn, day: date, tier: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM delivery_runs WHERE day = %s AND tier = %s", (day, tier))
        return cur.fetchone() is not None


def waiting_count(conn, tier: int) -> int:
    """Jobs of this tier still on their way to a score (the old 'deferred' count)."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs WHERE tier = %s AND stage IN ('seen', 'described')", (tier,))
        return cur.fetchone()[0]


def deliver_tier(conn, cfg: ProcessorConfig, settings: DeliverySettings, tier: int, day: date,
                 now: datetime) -> TierDelivery | None:
    """Deliver one tier for `day`. None when that tier was already delivered."""
    if already_delivered(conn, day, tier):
        return None
    policy = cfg.policies[tier]
    jobs = load_jobs(conn, "j.stage = 'scored' AND j.tier = %s", (tier,))
    rejected = load_jobs(
        conn, "j.stage = 'filtered_out' AND j.stage_reason LIKE 'remote_rejected%%' "
              "AND j.tier = %s AND j.delivery_day IS NULL", (tier,), order="j.first_seen_at")
    waiting = waiting_count(conn, tier)

    for job in jobs:
        if job.alerted_at is None or job.note_day is None:
            write_text(note_path(settings, job, day), render_note(job, policy.threshold, day))
    folder = day_folder(settings, day, tier)
    write_text(os.path.join(folder, "digest.md"),
               render_digest(jobs, day, policy.threshold, tier, policy.remote_check_enabled, waiting))
    if policy.remote_check_enabled:
        write_text(os.path.join(folder, "rejected.md"), render_rejected(rejected, day, tier))

    with conn.cursor() as cur:
        if jobs:
            cur.execute(
                "UPDATE jobs SET stage = 'delivered', delivered_at = %s, delivery_day = %s, "
                "note_day = COALESCE(note_day, %s), stage_changed_at = %s, archived_at = NULL "
                "WHERE id = ANY(%s::uuid[])", (now, day, day, now, [j.id for j in jobs]))
        if rejected:
            cur.execute("UPDATE jobs SET delivery_day = %s, archived_at = NULL "
                        "WHERE id = ANY(%s::uuid[])", (day, [j.id for j in rejected]))
        cur.execute("INSERT INTO delivery_runs (day, tier, completed_at, job_count) "
                    "VALUES (%s, %s, %s, %s)", (day, tier, now, len(jobs)))
    conn.commit()
    return TierDelivery(tier, day, jobs, rejected, waiting)


def delivered_jobs(conn, day: date, tier: int) -> list[DeliveryJob]:
    """What a given day's delivery contained (to resend its Telegram message)."""
    return load_jobs(conn, "j.delivery_day = %s AND j.tier = %s AND j.stage = 'delivered'", (day, tier))


def delivery_pending(conn, settings: DeliverySettings, tiers: list[int], now: datetime) -> date | None:
    """The day to deliver now, or None: delivery time has passed today and at
    least one tier has no delivery_runs row yet."""
    if not settings.delivery_started(now):
        return None
    day = settings.local_day(now)
    return day if any(not already_delivered(conn, day, tier) for tier in tiers) else None


def mark_telegram_sent(conn, day: date, tier: int, now: datetime) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE delivery_runs SET telegram_sent_at = %s WHERE day = %s AND tier = %s",
                    (now, day, tier))
    conn.commit()


def unsent_telegram(conn, day: date) -> list[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT tier FROM delivery_runs WHERE day = %s AND telegram_sent_at IS NULL "
                    "ORDER BY tier", (day,))
        return [row[0] for row in cur.fetchall()]
