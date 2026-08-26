import json
import os
from src.models import ScoredOffer
from src.telegram import send_message

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


def notify_package(offer: ScoredOffer, channel: str, directory: str, telegram_token: str, telegram_chat_id: str) -> None:
    """Notify the captain that a package is ready for manual review and submission.
    Sends a Telegram message via src/telegram.py (the same mechanism the daily digest
    uses), since production cron runs on a Linux VPS where the prior macOS-only
    osascript/open notification would silently no-op. Performs no submission of any
    kind."""
    label = _CHANNEL_LABELS.get(channel, channel)
    text = (
        "Application package ready to review\n\n"
        f"{offer.title} - {offer.company} ({label})\n"
        f"{offer.link}\n\n"
        f"Package: {directory}"
    )
    send_message(text, telegram_token, telegram_chat_id, parse_mode=None)
