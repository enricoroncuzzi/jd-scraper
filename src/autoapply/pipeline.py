import os
from datetime import date
from src.models import ScoredOffer
from src.writer import _note_filename
from src.autoapply.classify import classify_channel
from src.autoapply.package import write_manifest, notify_package
from src import storage
import tailor as tailor_cli


def _note_path(offer: ScoredOffer, output_path: str, tier: int, today: str) -> str:
    return os.path.join(output_path, today, f"tier{tier}", "scraped", _note_filename(offer))


def run_autoapply(
    offers: list[ScoredOffer],
    threshold: int,
    output_path: str,
    tier: int,
    db_url: str | None,
    cv_master_path: str,
    css_path: str,
    groq_api_key: str,
    daily_cap: int,
    dry_run: bool,
) -> list[dict]:
    """Auto-tailor and package every above-threshold, not-yet-packaged offer, up to
    `daily_cap` per day. Draft-and-notify only: this reuses tailor.py's existing
    tailoring/validation/PDF pipeline as-is and never submits an application anywhere.
    In dry-run mode the full pipeline (classify, tailor, package) runs, but no
    notification is sent and nothing is marked "packaged" in the tracking table."""
    today = date.today().isoformat()
    candidates = [o for o in offers if o.score >= threshold]
    if not candidates:
        return []

    already_today = storage.count_applications_packaged_today(db_url)
    budget = max(daily_cap - already_today, 0)

    results: list[dict] = []
    for offer in candidates:
        if budget <= 0:
            print(f"[autoapply] daily cap ({daily_cap}) reached, skipping remaining offers")
            break
        if storage.is_application_packaged(db_url, offer.link):
            continue

        channel = classify_channel(offer.link)
        storage.save_application_channel(db_url, offer.link, channel)

        note_path = _note_path(offer, output_path, tier, today)
        try:
            directory = tailor_cli.run(note_path, output_path, cv_master_path, css_path, groq_api_key)
        except Exception as e:
            print(f"[autoapply] tailoring failed for {offer.company}: {e}")
            continue

        write_manifest(directory, offer, channel, dry_run)
        budget -= 1

        if dry_run:
            print(f"[autoapply] dry-run: packaged {offer.company} without notifying or recording")
        else:
            notify_package(offer, channel, directory)
            storage.save_application(db_url, offer.link, offer.title, offer.company, channel, dry_run=False)

        results.append({"offer": offer, "channel": channel, "directory": directory, "dry_run": dry_run})

    return results
