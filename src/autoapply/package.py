import json
import os
from src.models import ScoredOffer
from src.tailor.notify import notify, reveal

_CHANNEL_LABELS = {
    "linkedin_easy_apply": "LinkedIn Easy Apply",
    "external_ats": "External ATS",
    "email_apply": "Email application",
    "unknown": "Unclassified",
}


def write_manifest(directory: str, offer: ScoredOffer, channel: str, dry_run: bool) -> str:
    """Write the per-offer application package manifest: everything the captain needs
    to review and submit by hand (link, classified channel, artifact paths). Never
    submits anything itself."""
    manifest = {
        "offer_id": offer.id,
        "title": offer.title,
        "company": offer.company,
        "score": offer.score,
        "link": offer.link,
        "channel": channel,
        "dry_run": dry_run,
        "artifacts": {
            "cv_pdf": os.path.join(directory, "Roncuzzi_CV.pdf"),
            "cover_letter_pdf": os.path.join(directory, "Roncuzzi_CL.pdf"),
            "hr_message": os.path.join(directory, "hr_message.txt"),
        },
    }
    path = os.path.join(directory, "APPLICATION_PACKAGE.json")
    with open(path, "w") as f:
        json.dump(manifest, f, indent=2)
    return path


def notify_package(offer: ScoredOffer, channel: str, directory: str) -> None:
    """Notify the captain that a package is ready for manual review and submission.
    Extends src/tailor/notify.py's existing macOS notification pattern; performs no
    submission of any kind."""
    label = _CHANNEL_LABELS.get(channel, channel)
    notify(
        "Application ready to review",
        f"{offer.title} — {offer.company} ({label})",
    )
    reveal(directory)
