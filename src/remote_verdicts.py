"""The remote check's verdict vocabulary, and what each verdict means downstream.

One word used to carry two different unknowns: `unconfirmed` meant both "the
description never states the work mode" and "the model could not tell whether an
Italy resident may take it". The captain split them (2026-10-18):

- `confirmed`         fully remote, workable from Italy (no eligibility problem)
- `advised`           fully remote, but limited to another country or with no
                      stated country restriction in a tier that needs Italy
                      eligibility. Never rejected for eligibility alone (D3);
                      delivered and alerted with a short advice line.
- `not_stated`        the text does not state the work mode (D1). Kept in the
                      digest in its own section, no Telegram Strong match alert.
- `rejected`          hybrid, on-site, or remote only as an occasional perk
- `not_checked`       the tier does not run the check at all (tier 2)
- `unconfirmed`       the pre-2026-10-18 value, kept readable and folded into
                      `not_stated` everywhere downstream.

This module is the single source of truth for that mapping, so the processor,
the digest renderers, the alerts and auto-packaging cannot drift apart.
"""
from __future__ import annotations

import re

from src.tier_scope import resolve_country

CONFIRMED = "confirmed"
ADVISED = "advised"
NOT_STATED = "not_stated"
REJECTED = "rejected"
NOT_CHECKED = "not_checked"
# Legacy value, still present in rows written before 2026-10-18 and still
# accepted by the jobs_remote_verdict_chk constraint. Never written anew.
LEGACY_UNCONFIRMED = "unconfirmed"

# Every value the store accepts, in the order the digest shows them.
ALL_VERDICTS = (CONFIRMED, ADVISED, NOT_STATED, REJECTED, NOT_CHECKED, LEGACY_UNCONFIRMED)
DELIVERED_VERDICTS = (CONFIRMED, ADVISED, NOT_STATED, NOT_CHECKED)
# What a rule or a model verdict is allowed to be. not_checked is assigned by the
# stage, never judged.
JUDGED_VERDICTS = (CONFIRMED, ADVISED, NOT_STATED, REJECTED)


def normalise(verdict: str | None) -> str:
    """Fold a stored value into its current meaning. A missing verdict came from
    a row the check never visited (or a pre-check row), which reads as not_checked;
    a legacy `unconfirmed` row reads as `not_stated`."""
    if verdict in (None, ""):
        return NOT_CHECKED
    if verdict == LEGACY_UNCONFIRMED:
        return NOT_STATED
    return verdict


def is_confirmed(verdict: str | None) -> bool:
    return normalise(verdict) == CONFIRMED


def is_advised(verdict: str | None) -> bool:
    return normalise(verdict) == ADVISED


def is_confirmed_or_unchecked(verdict: str | None) -> bool:
    """Confirmed, or a tier that runs no check at all. Both count as usable."""
    return normalise(verdict) in (CONFIRMED, NOT_CHECKED)


def is_not_stated(verdict: str | None) -> bool:
    return normalise(verdict) == NOT_STATED


def is_rejected(verdict: str | None) -> bool:
    return normalise(verdict) == REJECTED


def may_alert(verdict: str | None) -> bool:
    """D1: a job whose work mode is not stated never triggers a Telegram alert.
    Advised jobs do, carrying their advice line."""
    return normalise(verdict) not in (NOT_STATED, REJECTED)


def may_autopackage(verdict: str | None) -> bool:
    """Auto-tailoring stays on jobs the captain has decided are safe to spend a
    package on: confirmed remote, plus tier 2's not_checked (no check by design).
    Advised and not-stated jobs are drafted by hand, if at all."""
    return normalise(verdict) in (CONFIRMED, NOT_CHECKED)


# Digest / alert section labels. `advised` is a confirmed remote verdict, so it
# is grouped with the confirmed section in the alert but gets its own advice
# line; in the digest it gets its own section so the eligibility warning is
# impossible to miss.
SECTION_LABELS = {
    CONFIRMED: "Confirmed full-remote",
    ADVISED: "Remote, eligibility to check",
    NOT_STATED: "Work mode not stated",
}


def section_label(verdict: str | None) -> str:
    return SECTION_LABELS.get(normalise(verdict), "Other")


# Country names we can name in an advice line, and the short display form.
# Ordered so multi-word and alias forms win over their substrings.
_COUNTRY_DISPLAY: tuple[tuple[str, str], ...] = (
    ("united kingdom", "the UK"),
    ("great britain", "the UK"),
    ("uk", "the UK"),
    ("united states", "the US"),
    ("usa", "the US"),
    ("us", "the US"),
    ("the netherlands", "the Netherlands"),
    ("netherlands", "the Netherlands"),
    ("holland", "the Netherlands"),
    ("switzerland", "Switzerland"),
    ("germany", "Germany"),
    ("france", "France"),
    ("spain", "Spain"),
    ("portugal", "Portugal"),
    ("italy", "Italy"),
    ("poland", "Poland"),
    ("romania", "Romania"),
    ("ireland", "Ireland"),
    ("canada", "Canada"),
    ("india", "India"),
    ("australia", "Australia"),
    ("brazil", "Brazil"),
    ("latam", "Latin America"),
    ("singapore", "Singapore"),
    ("japan", "Japan"),
    ("china", "China"),
    ("israel", "Israel"),
    ("turkey", "Turkey"),
    ("denmark", "Denmark"),
    ("sweden", "Sweden"),
    ("norway", "Norway"),
    ("finland", "Finland"),
    ("austria", "Austria"),
    ("belgium", "Belgium"),
    ("czechia", "Czechia"),
    ("greece", "Greece"),
    ("hungary", "Hungary"),
    ("luxembourg", "Luxembourg"),
    ("malta", "Malta"),
    ("cyprus", "Cyprus"),
)


_US_PATTERNS = (
    r"(?<![A-Z]{2}\s)\bUS\b(?!\s+[A-Z]{2,}\b)",
    r"(?i)\(\s*us\s*\)",
    r"(?i)\b(?:in|within|inside)\s+the\s+us\b",
    r"(?i)\bus-based\b",
)


def country_in(text: str) -> str | None:
    """The first known country named in `text`, as a short display name, or None.

    Two-letter forms are checked separately: "uk" appears inside words, so it only
    counts uppercase, in parentheses, or after a preposition; "us" is also an
    English pronoun ("of us", "ABOUT US"), so it only counts as an uppercase word
    outside an all-caps phrase, "(US)", "in/within the US" or "US-based". Nothing here is used to reject a job - only to word an
    advice line - so a miss costs a vaguer line, never a lost posting.
    """
    lowered = text.lower()
    for needle, display in _COUNTRY_DISPLAY:
        if needle == "us":
            if any(re.search(pattern, text) for pattern in _US_PATTERNS):
                return display
        elif needle == "uk":
            if (re.search(rf"\b{needle.upper()}\b", text)
                    or re.search(rf"\(\s*{needle}\s*\)", text, re.IGNORECASE)
                    or re.search(rf"\b(?:in|within|of|to|from|inside)\s+(?:the\s+)?{needle}\b",
                                 text, re.IGNORECASE)):
                return display
        elif re.search(rf"(?<![a-z]){re.escape(needle)}(?![a-z])", lowered):
            return display
    return None


def advice_line(verdict: str | None, remote_advice: str = "", remote_reason: str = "") -> str:
    """The short line shown for an advised job. Prefers the advice the stage
    stored; falls back to a country named in the reason, then a generic line."""
    if not is_advised(verdict):
        return ""
    if remote_advice.strip():
        return remote_advice.strip()
    country = country_in(remote_reason)
    if country:
        return f"remote, limited to {country} - check eligibility"
    return "remote, eligibility to check"


def no_restriction_advice() -> str:
    return "remote, no stated country restriction - check eligibility"


def limited_to_advice(country: str) -> str:
    return f"remote, limited to {country} - check eligibility"


def based_in_advice(country: str) -> str:
    return f"remote, based in {country} - check eligibility"


def unrestricted_advice(location: str) -> str:
    """The advice for a remote job that states no country restriction, shared by
    the rule layer and the model path so both word the same posting the same way:
    the location's country when it names one other than Italy, else the generic line."""
    resolved = resolve_country(location)
    country = (country_in(resolved) if resolved else None) or country_in(location or "")
    if country and country != "Italy":
        return based_in_advice(country)
    return no_restriction_advice()
