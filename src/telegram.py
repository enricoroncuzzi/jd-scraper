from datetime import date
import requests
from src.models import ScoredOffer

_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
_MAX_CHARS = 4096
_TOP_N = 5


def send_summary(
    offers: list[ScoredOffer],
    threshold: int,
    greeting: str,
    token: str,
    chat_id: str,
) -> None:
    text = _format_message(offers, threshold, greeting)
    url = _API_URL.format(token=token)
    response = requests.post(
        url,
        json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
        timeout=10,
    )
    if not response.ok:
        print(f"[telegram] Send failed: {response.status_code} {response.text}")


def _format_message(offers: list[ScoredOffer], threshold: int, greeting: str) -> str:
    today = date.today().isoformat()
    high = [o for o in offers if o.score >= threshold]
    low = [o for o in offers if o.score < threshold]

    if not offers:
        return f"{greeting}\n\nJob Digest — {today}\n\nNo new offers after dedup filter."

    lines = [f"{greeting}\n\nJob Digest — {today}\n"]

    if high:
        ranked = sorted(high, key=lambda x: x.score, reverse=True)
        shown, rest = ranked[:_TOP_N], ranked[_TOP_N:]
        lines.append(f"*High-score offers ({len(high)}):*\n")
        for o in shown:
            lines.append(f"• *{o.title} — {o.company}* ({o.score}/10)")
            lines.append(f"  {o.location}")
            lines.append(f"  {o.summary}")
            lines.append(f"  {o.link}\n")
        if rest:
            lines.append(f"_+{len(rest)} more above threshold — check vault for full list._\n")

    if low:
        lines.append(f"Low-score: {len(low)} offers below threshold. Check vault for notes.")

    return "\n".join(lines)
