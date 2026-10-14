"""Digest, notes and rejected.md, byte-compatible with src/writer.py.

The formatting is delegated to the writer's own functions, so the output stays
identical to today's by construction; this module only adds what the redesign
adds: the `source` and `also_seen_on` frontmatter fields, the "sent earlier"
marker, and links that point at the day folder an alerted note was written in.
tests/test_delivery_golden.py compares the result with src/writer.py output.
"""
from datetime import date

from src.delivery.jobs import DeliveryJob, to_scored_offer
from src.writer import _format_digest, _format_note, _note_filename

OFFER_CAP = 20


def note_filename(job: DeliveryJob) -> str:
    return _note_filename(to_scored_offer(job))


def render_note(job: DeliveryJob, threshold: int, note_day: date) -> str:
    tag = "high-score" if job.score >= threshold else "low-score"
    text = _format_note(to_scored_offer(job), tag, note_day.isoformat(), job.tier)
    extra = f"source: {job.source}\nalso_seen_on: [{', '.join(job.also_seen_on)}]\n"
    return text.replace("\ntags: [", "\n" + extra + "tags: [", 1)


def _mark_alerted_entry(line: str, job: DeliveryJob, digest_day: date) -> str:
    """An entry for a job an immediate alert already sent. When the alert wrote the
    note in another day's folder, the links point there instead."""
    if job.note_day is not None and job.note_day != digest_day:
        note_day, today = job.note_day.isoformat(), digest_day.isoformat()
        file = note_filename(job)
        line = line.replace(f"[note](scraped/{file})",
                            f"[note](../../{note_day}/tier{job.tier}/scraped/{file})")
        for prefix in ("tailor:", "jdlabel:golden/", "jdlabel:not_for_me/"):
            line = line.replace(f"{prefix}{today}/", f"{prefix}{note_day}/")
    return f"{line} · sent earlier"


def render_digest(jobs: list[DeliveryJob], digest_day: date, threshold: int, tier: int,
                  verification_enabled: bool, deferred_count: int = 0) -> str:
    text = _format_digest([to_scored_offer(job) for job in jobs], digest_day.isoformat(), threshold,
                          tier, OFFER_CAP, verification_enabled, deferred_count)
    alerted = {note_filename(job): job for job in jobs if job.alerted_at is not None}
    if not alerted:
        return text
    lines = []
    for line in text.split("\n"):
        if line.startswith("- **"):
            for file, job in alerted.items():
                if f"[note](scraped/{file})" in line:
                    line = _mark_alerted_entry(line, job, digest_day)
                    break
        lines.append(line)
    return "\n".join(lines)


def render_rejected(jobs: list[DeliveryJob], digest_day: date, tier: int) -> str:
    """The audit trail of what the remote check threw out, written even when empty."""
    lines = [f"# Rejected by remote verification - {digest_day.isoformat()} (tier {tier})\n"]
    if not jobs:
        lines.append("None. Every offer was confirmed or left unconfirmed.\n")
    else:
        for job in jobs:
            lines.append(f"- **{job.title} - {job.company}** · {job.location_raw} · "
                         f"[link]({job.canonical_url})")
            lines.append(f"  {job.remote_reason}\n")
    return "\n".join(lines)
