"""Immediate alerts for strong matches (spec section 7.2).

A scored job at or above its tier's threshold gets, within a minute of scoring: its
note written into today's folder (so a jd-sync after the alert brings it to the
Mac) and a short Telegram message. It still appears in the next daily digest,
marked "sent earlier". Sending happens before alerted_at is set, so a crash can
only repeat an alert, never lose one.

D1 (2026-10-18): a job whose description never states the work mode stays in the
daily digest but never triggers one of these alerts. An advised job (remote,
eligibility to check) does alert, carrying its short advice line.
"""
from datetime import datetime

from src import remote_verdicts
from src.delivery.daily import note_path, write_text
from src.delivery.jobs import DeliveryJob, load_jobs
from src.delivery.render import render_note
from src.delivery.settings import DeliverySettings
from src.processor.config import ProcessorConfig


def format_alert(job: DeliveryJob) -> str:
    # Confirmed and tier 2's not_checked have no advice; an advised job says why
    # in one short line instead of the old "(remote not confirmed)" tag.
    advice = remote_verdicts.advice_line(job.remote_verdict, job.remote_advice, job.remote_reason)
    suffix = f"\n{advice}" if advice else ""
    return (f"Strong match {job.score}/10 - tier {job.tier}{suffix}\n"
            f"{job.title} - {job.company}\n{job.summary}\n{job.canonical_url}")


def due_alerts(conn, cfg: ProcessorConfig) -> list[DeliveryJob]:
    # D1: not-stated jobs are digest-only. The legacy `unconfirmed` value means
    # the same thing. Tier 2 runs no check and is not filtered (its verdict is
    # not_checked); COALESCE covers the same case for a row the stage has not
    # reached yet.
    conditions = " OR ".join(
        "(j.tier = %s AND j.score >= %s"
        + ("" if not policy.remote_check_enabled else
           " AND COALESCE(j.remote_verdict, 'not_checked') NOT IN ('not_stated', 'unconfirmed')")
        + ")"
        for tier, policy in sorted(cfg.policies.items()))
    params = tuple(value for tier, policy in sorted(cfg.policies.items()) for value in (tier, policy.threshold))
    return load_jobs(conn, f"j.stage = 'scored' AND j.alerted_at IS NULL AND ({conditions})", params,
                     order="j.score DESC, j.first_seen_at")


def run_alerts(conn, cfg: ProcessorConfig, settings: DeliverySettings, send, now: datetime,
               on_alerted=None) -> int:
    """Alert every due job. `send(text)` raises or returns False on failure, which
    stops this round and leaves the job due for the next. Returns jobs alerted."""
    sent = 0
    day = settings.local_day(now)
    for job in due_alerts(conn, cfg):
        policy = cfg.policies[job.tier]
        note_day = job.note_day or day
        if job.note_day is None:
            write_text(note_path(settings, job, note_day), render_note(job, policy.threshold, note_day))
            with conn.cursor() as cur:
                cur.execute("UPDATE jobs SET note_day = %s WHERE id = %s", (note_day, job.id))
            conn.commit()
            job.note_day = note_day
        try:
            if send(format_alert(job)) is False:
                raise RuntimeError("Telegram rejected the message")
        except Exception as exc:  # noqa: BLE001 - a failed send is retried next round
            print(f"[deliver] alert for {job.title} failed ({type(exc).__name__}: {exc}); will retry")
            break
        with conn.cursor() as cur:
            cur.execute("UPDATE jobs SET alerted_at = %s, archived_at = NULL WHERE id = %s", (now, job.id))
        conn.commit()
        sent += 1
        if on_alerted is not None:
            try:
                on_alerted(job)
            except Exception as exc:  # noqa: BLE001 - packaging is retried by the daily pass
                print(f"[deliver] packaging after the alert failed ({type(exc).__name__}: {exc})")
    return sent
