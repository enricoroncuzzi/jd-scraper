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
    verification_enabled: bool = False,
    verification_degraded: bool = False,
) -> None:
    text = _format_message(offers, threshold, greeting, verification_enabled, verification_degraded)
    send_message(text, token, chat_id)


def send_message(text: str, token: str, chat_id: str, parse_mode: str | None = "Markdown") -> None:
    url = _API_URL.format(token=token)
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    response = requests.post(
        url,
        json=payload,
        timeout=10,
    )
    if not response.ok:
        print(f"[telegram] Send failed: {response.status_code} {response.text}")


def _offer_block(o: ScoredOffer) -> list[str]:
    return [
        f"- *{o.title} - {o.company}* ({o.score}/10)",
        f"  {o.location}",
        f"  {o.summary}",
        f"  {o.link}\n",
    ]


def _append_section(lines: list[str], header: str, ranked: list[ScoredOffer]) -> None:
    shown, rest = ranked[:_TOP_N], ranked[_TOP_N:]
    lines.append(f"{header}\n")
    for o in shown:
        lines.extend(_offer_block(o))
    if rest:
        lines.append(f"_+{len(rest)} more above threshold - check vault for full list._\n")


def _format_message(
    offers: list[ScoredOffer],
    threshold: int,
    greeting: str,
    verification_enabled: bool = False,
    verification_degraded: bool = False,
) -> str:
    today = date.today().isoformat()
    high = [o for o in offers if o.score >= threshold]
    low = [o for o in offers if o.score < threshold]

    if not offers:
        return f"{greeting}\n\nJob Digest - {today}\n\nNo new offers after dedup filter."

    lines = [f"{greeting}\n\nJob Digest - {today}\n"]

    if high:
        ranked = sorted(high, key=lambda x: x.score, reverse=True)
        if not verification_enabled:
            _append_section(lines, f"*High-score offers ({len(high)}):*", ranked)
        else:
            confirmed = [o for o in ranked if o.remote_verdict == "confirmed"]
            unconfirmed = [o for o in ranked if o.remote_verdict != "confirmed"]
            if confirmed:
                _append_section(lines, f"*Confirmed full-remote ({len(confirmed)}):*", confirmed)
            if unconfirmed:
                _append_section(lines, f"*Remote not confirmed ({len(unconfirmed)}):*", unconfirmed)

    if low:
        lines.append(f"Low-score: {len(low)} offers below threshold. Check vault for notes.")

    if verification_degraded:
        lines.append("\n_Remote verification did not run for this tier; treat every offer as unconfirmed._")

    return "\n".join(lines)
