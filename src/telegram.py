from dataclasses import dataclass
from datetime import date
import requests
from src import remote_verdicts
from src.models import ScoredOffer

_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
# Telegram's documented maximum for one message's text. A longer text is refused
# whole with 400 "Bad Request: message is too long" (2026-11-02 tier 1 digest).
MAX_MESSAGE_CHARS = 4096
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
    return send_message_reply(text, token, chat_id, parse_mode=parse_mode).ok


@dataclass(frozen=True)
class TelegramReply:
    """What Telegram's answer means for the caller. `status_code` is None when no
    answer arrived (a network error or a timeout), so this is not just a bool: a
    4xx answer to some text will never succeed on a retry of the same text, while
    an outage or a 5xx is transient."""
    ok: bool
    status_code: int | None = None

    @property
    def refused(self) -> bool:
        """A 4xx answer: resending the same text cannot succeed."""
        return not self.ok and self.status_code is not None and 400 <= self.status_code < 500

    def __bool__(self) -> bool:
        return self.ok


def as_reply(value) -> TelegramReply:
    """Normalise a sender's answer: a test's fake sender returns a plain bool."""
    return value if isinstance(value, TelegramReply) else TelegramReply(bool(value))


def send_message_reply(text: str, token: str, chat_id: str,
                       parse_mode: str | None = "Markdown") -> TelegramReply:
    """Like send_message, but keeps Telegram's status code. An exception from the
    request still propagates: the caller decides whether it can retry."""
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
    return TelegramReply(bool(response.ok), response.status_code)


def _offer_block(o: ScoredOffer) -> str:
    lines = [
        f"- <b>{escape_html(o.title)} - {escape_html(o.company)}</b> ({o.score}/10)",
        f"  {escape_html(o.location)}",
        f"  {escape_html(o.summary)}",
    ]
    advice = remote_verdicts.advice_line(o.remote_verdict, o.remote_advice, o.remote_reason)
    if advice:
        # D3: remote but the eligibility needs a human look. Delivered, not dropped.
        lines.append(f"  ⚠ {escape_html(advice)}")
    lines.append(f"  {escape_html(o.link)}")
    return "\n".join(lines)


def _append_section(blocks: list[str], header: str, ranked: list[ScoredOffer], budget: int) -> int:
    shown, rest = ranked[:budget], ranked[budget:]
    blocks.append(header)
    for o in shown:
        blocks.append(_offer_block(o))
    if rest:
        blocks.append(f"<i>+{len(rest)} more above threshold - check vault for full list.</i>")
    return budget - len(shown)


def _format_message(
    offers: list[ScoredOffer],
    threshold: int,
    greeting: str,
    verification_enabled: bool = False,
    verification_degraded: bool = False,
    deferred_count: int = 0,
) -> str:
    return "\n\n".join(message_blocks(
        offers, threshold, greeting, verification_enabled, verification_degraded, deferred_count))


def message_blocks(
    offers: list[ScoredOffer],
    threshold: int,
    greeting: str,
    verification_enabled: bool = False,
    verification_degraded: bool = False,
    deferred_count: int = 0,
) -> list[str]:
    """The digest as blocks: one per section header and one per offer, joined by a
    blank line. Splitting a message between blocks therefore never cuts an offer
    in half and every block carries its own balanced HTML tags."""
    today = date.today().isoformat()
    high = [o for o in offers if o.score >= threshold]
    low = [o for o in offers if o.score < threshold]

    if not offers:
        # An empty digest has two very different causes and they must not read
        # the same: nothing found, versus offers found that scoring never
        # reached (see src/retry_queue.py). Claiming "no new offers after
        # dedup" on a day 222 offers went unscored hid a real loss.
        if deferred_count:
            return [
                f"{escape_html(greeting)}\n\nJob Digest - {today}\n\n"
                f"No offers scored today: {deferred_count} offer(s) are still unscored and "
                f"queued for the next run (scoring did not finish)."
            ]
        return [f"{escape_html(greeting)}\n\nJob Digest - {today}\n\nNo new offers after dedup filter."]

    blocks = [f"{escape_html(greeting)}\n\nJob Digest - {today}"]

    if high:
        ranked = sorted(high, key=lambda x: x.score, reverse=True)
        if not verification_enabled:
            _append_section(blocks, f"<b>High-score offers ({len(high)}):</b>", ranked, _TOP_N)
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
                    blocks, f"<b>Confirmed full-remote ({len(confirmed)}):</b>", confirmed, budget
                )
            if advised:
                budget = _append_section(
                    blocks, f"<b>Remote, eligibility to check ({len(advised)}):</b>",
                    advised, budget
                )
            if not_stated:
                # D1: kept in the digest, clearly separated, and never alerted.
                _append_section(
                    blocks, f"<b>Work mode not stated ({len(not_stated)}):</b>",
                    not_stated, budget
                )

    if low:
        blocks.append(f"Low-score: {len(low)} offers below threshold. Check vault for notes.")

    if deferred_count:
        blocks.append(
            f"<i>{deferred_count} offer(s) still unscored and queued for the next run "
            f"(scoring stopped early).</i>"
        )

    if verification_degraded:
        blocks.append(
            "<i>Remote verification was incomplete for this tier; some offers may show "
            "'work mode not stated' because a stage failure, not a real judgement, left them "
            "that way.</i>"
        )

    return blocks
