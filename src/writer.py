import os
import re
from datetime import date
from src.models import ScoredOffer


def _note_filename(offer: ScoredOffer) -> str:
    return f"{_slugify(offer.company)}_{_slugify(offer.title)}_{offer.id}.md"


def write_notes(offers: list[ScoredOffer], output_path: str, threshold: int, tier: int) -> None:
    today = date.today().isoformat()
    scraped_dir = os.path.join(output_path, today, f"tier{tier}", "scraped")
    os.makedirs(scraped_dir, exist_ok=True)
    for offer in offers:
        tag = "high-score" if offer.score >= threshold else "low-score"
        filename = _note_filename(offer)
        path = os.path.join(scraped_dir, filename)
        try:
            with open(path, "w") as f:
                f.write(_format_note(offer, tag, today, tier))
        except OSError as e:
            print(f"[writer] Failed to write {path}: {e}")


def write_digest(offers: list[ScoredOffer], output_path: str, threshold: int, tier: int, offer_cap: int = 20) -> None:
    today = date.today().isoformat()
    tier_dir = os.path.join(output_path, today, f"tier{tier}")
    os.makedirs(tier_dir, exist_ok=True)
    for mode in ("remote", "hybrid"):
        mode_offers = [o for o in offers if o.work_mode == mode]
        path = os.path.join(tier_dir, f"digest_{mode}.md")
        try:
            with open(path, "w") as f:
                f.write(_format_digest(mode_offers, today, threshold, offer_cap))
        except OSError as e:
            print(f"[writer] Failed to write {path}: {e}")


def _slugify(text: str, max_len: int = 40) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", "", text)
    text = re.sub(r"\s+", "_", text.strip())
    return text[:max_len]


def _format_note(offer: ScoredOffer, tag: str, today: str, tier: int) -> str:
    warning = (
        f"\n⚠ description: {offer.description_status}"
        if offer.description_status != "ok"
        else ""
    )
    return (
        f"---\n"
        f"date: {today}\n"
        f"tier: {tier}\n"
        f"work_mode: {offer.work_mode}\n"
        f"score: {offer.score}\n"
        f"company: {offer.company}\n"
        f"location: {offer.location}\n"
        f"link: {offer.link}\n"
        f"tags: [job, scraped, {tag}]\n"
        f"---\n\n"
        f"# {offer.title} — {offer.company}\n\n"
        f"**Location:** {offer.location}\n"
        f"**Score:** {offer.score}/10\n"
        f"**Comment:** {offer.comment}\n"
        f"**Summary:** {offer.summary}\n"
        f"**Link:** {offer.link}\n"
        f"**Scraped:** {today}\n"
        f"{warning}\n\n"
        f"## Job Description\n\n"
        f"{offer.description}\n"
    )


def _format_digest(offers: list[ScoredOffer], today: str, threshold: int, offer_cap: int) -> str:
    if not offers:
        return f"# Job Digest — {today}\n\nNo new offers after dedup filter.\n"

    high = sorted([o for o in offers if o.score >= threshold], key=lambda x: x.score, reverse=True)[:offer_cap]
    low = [o for o in offers if o.score < threshold]
    lines = [f"# Job Digest — {today}\n"]

    if high:
        lines.append(f"## High-Score Offers (≥{threshold})\n")
        for o in high:
            note_file = _note_filename(o)
            lines.append(f"- **{o.title} — {o.company}** ({o.score}/10) · {o.location} · [link]({o.link}) · [note](scraped/{note_file})")
            lines.append(f"  {o.summary}\n")

    if low:
        lines.append(f"## Low-Score Offers\n")
        lines.append(f"{len(low)} offers below threshold. Notes written to scraped/.\n")

    return "\n".join(lines)
