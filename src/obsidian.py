import os
import re
from datetime import date
from src.models import ScoredOffer


def write_notes(offers: list[ScoredOffer], vault_path: str, threshold: int) -> None:
    today = date.today().isoformat()
    scraped_dir = os.path.join(vault_path, "jobs", f"scraped_{today.replace('-', '')}")
    os.makedirs(scraped_dir, exist_ok=True)
    for offer in offers:
        tag = "high-score" if offer.score >= threshold else "low-score"
        filename = f"{today}_{_slugify(offer.company)}_{_slugify(offer.title)}.md"
        path = os.path.join(scraped_dir, filename)
        try:
            with open(path, "w") as f:
                f.write(_format_note(offer, tag, today))
        except OSError as e:
            print(f"[obsidian] Failed to write {path}: {e}")


def write_digest(offers: list[ScoredOffer], vault_path: str, threshold: int) -> None:
    today = date.today().isoformat()
    digest_dir = os.path.join(vault_path, "jobs", "digest")
    os.makedirs(digest_dir, exist_ok=True)
    path = os.path.join(digest_dir, f"{today}_digest.md")
    with open(path, "w") as f:
        f.write(_format_digest(offers, today, threshold))


def _slugify(text: str, max_len: int = 40) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", "", text)
    text = re.sub(r"\s+", "_", text.strip())
    return text[:max_len]


def _format_note(offer: ScoredOffer, tag: str, today: str) -> str:
    return (
        f"---\n"
        f"date: {today}\n"
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
        f"**Scraped:** {today}\n\n"
        f"## Job Description\n\n"
        f"{offer.description}\n"
    )


def _format_digest(offers: list[ScoredOffer], today: str, threshold: int) -> str:
    if not offers:
        return f"# Job Digest — {today}\n\nNo new offers after dedup filter.\n"

    high = [o for o in offers if o.score >= threshold]
    low = [o for o in offers if o.score < threshold]
    lines = [f"# Job Digest — {today}\n"]

    if high:
        lines.append(f"## High-Score Offers (≥{threshold})\n")
        for o in sorted(high, key=lambda x: x.score, reverse=True):
            note_name = f"{today}_{_slugify(o.company)}_{_slugify(o.title)}"
            lines.append(f"- **{o.title} — {o.company}** ({o.score}/10) · {o.location} · [link]({o.link}) · [[{note_name}]]")
            lines.append(f"  {o.summary}\n")

    if low:
        lines.append(f"## Low-Score Offers\n")
        lines.append(f"{len(low)} offers below threshold. Notes written to jobs/scraped/.\n")

    return "\n".join(lines)
