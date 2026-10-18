from datetime import date
import requests
from src import remote_verdicts
from src.models import ScoredOffer

_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
_MAX_CHARS = 4096
_TOP_N = 5

# Telegram's HTML parse mode. The digest used legacy "Markdown", but tdlib's
# parse_markdown only honours backslash escapes at the top level, so a title
# sitting inside a `*bold*` entity (or any unbalanced `_ * ` [`) either
# mangled the message or made Telegram reject it whole with 400 "can't parse
# entities" - the 2026-10-15 morning digest. HTML escapes are honoured
# everywhere, including inside <b>/<i>, so dynamic text can never break the
# message and the rendered format is unchanged.
HTML_PARSE_MODE = "HTML"


def escape_html(text: str) -> str:
    """Escape the three characters Telegram's HTML parse mode reserves, so a
    job title or company can never break the message or inject a tag."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_summary(
    offers: list[ScoredOffer],
    threshold: int,
    greeting: str,
    token: str,
    chat_id: str,
    verification_enabled: bool = False,
    verification_degraded: bool = False,
    deferred_count: int = 0,
) -> None:
    text = _format_message(
        offers, threshold, greeting, verification_enabled, verification_degraded, deferred_count
    )
    send_message(text, token, chat_id, parse_mode=HTML_PARSE_MODE)


def send_message(text: str, token: str, chat_id: str, parse_mode: str | None = "Markdown") -> bool:
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
        return False
    return True


def _offer_block(o: ScoredOffer) -> list[str]:
    lines = [
        f"- <b>{escape_html(o.title)} - {escape_html(o.company)}</b> ({o.score}/10)",
        f"  {escape_html(o.location)}",
        f"  {escape_html(o.summary)}",
    ]
    advice = remote_verdicts.advice_line(o.remote_verdict, o.remote_advice, o.remote_reason)
    if advice:
        # D3: remote but the eligibility needs a human look. Delivered, not dropped.
        lines.append(f"  ⚠ {escape_html(advice)}")
    lines.append(f"  {escape_html(o.link)}\n")
    return lines


def _append_section(lines: list[str], header: str, ranked: list[ScoredOffer], budget: int) -> int:
    shown, rest = ranked[:budget], ranked[budget:]
    lines.append(f"{header}\n")
    for o in shown:
        lines.extend(_offer_block(o))
    if rest:
        lines.append(f"<i>+{len(rest)} more above threshold - check vault for full list.</i>\n")
    return budget - len(shown)


def _format_message(
    offers: list[ScoredOffer],
    threshold: int,
    greeting: str,
    verification_enabled: bool = False,
    verification_degraded: bool = False,
    deferred_count: int = 0,
) -> str:
    today = date.today().isoformat()
    high = [o for o in offers if o.score >= threshold]
    low = [o for o in offers if o.score < threshold]

    if not offers:
        # An empty digest has two very different causes and they must not read
        # the same: nothing found, versus offers found that scoring never
        # reached (see src/retry_queue.py). Claiming "no new offers after
        # dedup" on a day 222 offers went unscored hid a real loss.
        if deferred_count:
            return (
                f"{escape_html(greeting)}\n\nJob Digest - {today}\n\n"
                f"No offers scored today: {deferred_count} offer(s) are still unscored and "
                f"queued for the next run (scoring did not finish)."
            )
        return f"{escape_html(greeting)}\n\nJob Digest - {today}\n\nNo new offers after dedup filter."

    lines = [f"{escape_html(greeting)}\n\nJob Digest - {today}\n"]

    if high:
        ranked = sorted(high, key=lambda x: x.score, reverse=True)
        if not verification_enabled:
            _append_section(lines, f"<b>High-score offers ({len(high)}):</b>", ranked, _TOP_N)
        else:
            confirmed = [o for o in ranked
                         if remote_verdicts.is_confirmed_or_unchecked(o.remote_verdict)]
            advised = [o for o in ranked if remote_verdicts.is_advised(o.remote_verdict)]
            not_stated = [o for o in ranked if remote_verdicts.is_not_stated(o.remote_verdict)]
            # One detailed-block budget shared by the sections, so the message
            # cannot double in size when verification is on. Confirmed offers
            # hold the budget first; the other sections get what is left.
            budget = _TOP_N
            if confirmed:
                budget = _append_section(
                    lines, f"<b>Confirmed full-remote ({len(confirmed)}):</b>", confirmed, budget
                )
            if advised:
                budget = _append_section(
                    lines, f"<b>Remote, eligibility to check ({len(advised)}):</b>",
                    advised, budget
                )
            if not_stated:
                # D1: kept in the digest, clearly separated, and never alerted.
                _append_section(
                    lines, f"<b>Work mode not stated ({len(not_stated)}):</b>",
                    not_stated, budget
                )

    if low:
        lines.append(f"Low-score: {len(low)} offers below threshold. Check vault for notes.")

    if deferred_count:
        lines.append(
            f"\n<i>{deferred_count} offer(s) still unscored and queued for the next run "
            f"(scoring stopped early).</i>"
        )

    if verification_degraded:
        lines.append(
            "\n<i>Remote verification was incomplete for this tier; some offers may show "
            "'work mode not stated' because a stage failure, not a real judgement, left them "
            "that way.</i>"
        )

    return "\n".join(lines)
