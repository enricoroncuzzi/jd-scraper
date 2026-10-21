"""Deterministic work-mode pre-pass, run before any model call.

The scout (2026-10-18) measured that the description text itself settles most
jobs, and that the model reads the same phrases the same way. This module
recognises only phrases that mean one thing, in English, Italian, Spanish,
French and German:

- a confident hybrid / on-site phrase, or the source's own hybrid/onsite work
  mode, is a rejection. Bare words that also occur in non-employment senses
  ("hybrid search", "hybrid cloud", "on-site interview", "hybrid role" meaning
  mixed duties) are deliberately NOT in the confident list.
- a confident fully-remote phrase ("fully remote", "remote-first", "100%
  teletrabajo", "vollstaendig remote") confirms the work mode. In a tier that
  needs Italy eligibility the rules settle eligibility only in two explicit
  cases (see `explicit_eligibility`): "Remote (UK)" is `advised` (D3) and
  "Remote - Italy" is confirmed. Every other remote posting there goes to the
  model, which answers work mode and eligibility together: residency wording
  ("based in", "only", "citizens", "right to work") is too often about the
  company, its team or its clients for a regex to read it.
- "remote" mentioned only as a perk or an option (smart working, WFH days,
  "hybrid or remote", "flexible working") is borderline by definition and goes
  to the model.
- text with no work-mode vocabulary at all is `not_stated` (D1) without a model
  call. The scout found 0 of 50 silent descriptions were genuinely remote; the
  phrase list below is deliberately wide so unusual wording still reaches the
  model rather than being written off.

Anything this module is not sure about returns None, and the existing batched
verifier decides.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from src.remote_verdicts import (
    ADVISED,
    CONFIRMED,
    NOT_STATED,
    REJECTED,
    countries_in,
    limited_to_advice,
)

RULE_PREFIX = "rule: "
# Below this the description is a fragment (a failed fetch, a title-only note),
# and "no work-mode phrase" would be an artefact of the truncation, not a fact
# about the posting. Those go to the model.
_MIN_DESCRIPTION_CHARS = 200

# "25 days of holiday, home office budget" is a benefits list, not office days:
# the gap between the day count and the office word may not cross a leave word
# or "home", and the office word may not be a budget or an allowance.
_WORKPLACE_GAP = (r"(?:(?!holiday|leave|vacation|annual|ferie|vacanz|urlaub|vacaciones|cong[eé]|home)"
                  r"[^.]){0,40}")
_NOT_A_PERK = r"(?!\s*(?:budget|allowance|equipment|stipend))"

# Confident hybrid / on-site. Every alternative is a workplace sentence, not a
# word that only happens to be spelled the same way: bare words such as
# "presenza", "ibrido", "hybride", "Präsenz" or "vor Ort" also mean a market
# presence, a hybrid cloud or a customer site, so they only reach the model.
_CONFIDENT_HYBRID = re.compile(
    "|".join([
        r"#li-?hybrid",
        r"fully\s+on-?site",
        r"100\s*%\s*on-?site",
        r"\b(?:role|position|job)\s+is\s+(?:fully\s+)?on-?site",
        r"on-?site\s+(?:role|position|job|work|presence|attendance|days?)",
        r"in-?office\s+(?:role|position|work|presence|attendance|days?)",
        r"office-?based",
        r"hybrid\s+work(?:ing)?\s+(?:model|pattern|policy|setup|set-?up|arrangement|schedule|mode|basis|environment)",
        r"(?:our|the|a)\s+hybrid\s+(?:model|approach|setup|set-?up|arrangement)\s+(?:to|of|for|where|with|in|is|means)\b",
        r"hybrid\s+(?:schedule|set-?up|basis|environment|arrangement)",
        r"work(?:ing)?\s+(?:in|on)\s+a\s+hybrid",
        rf"minimum\s+of\s+\w+\s+days?{_WORKPLACE_GAP}(?:office|on-?site|ufficio|sede){_NOT_A_PERK}",
        r"(?:\d+|two|three|four|five)\s*\+?\s*(?:days?|giorni|d[ií]as|jours|tage)"
        rf"{_WORKPLACE_GAP}(?:office|ufficio|sede|oficina|bureau|b[uü]ro|on-?site){_NOT_A_PERK}",
        r"(?:one|two|three|four|five)\s+days?\s+(?:a|per)\s+week\s+in\s+(?:the|our)\s+office",
        r"in\s+ufficio",
        r"\bin\s+sede\b(?!\s+di)",
        r"modalit[aà]\s+ibrid",
        r"lavoro\s+ibrido",
        r"modelo\s+h[ií]brido",
        r"modalidad\s+h[ií]brida",
        r"\bsur\s+site\b",
        r"pr[eé]sentiel",
    ]),
    re.IGNORECASE,
)

# Confident fully-remote. Same rule: no bare "remote" (it names remote sensing,
# remote support, a remote team mate), only phrasing that states the mode.
_CONFIDENT_REMOTE = re.compile(
    "|".join([
        r"fully[- ]remote",
        r"full[- ]remot[eo]",
        r"100\s*%\s*(?:remote|remoto|teletrabajo|t[ée]l[ée]travail|homeoffice|home\s*office|distanciel)",
        r"remote[- ]first",
        r"remote[- ]only",
        r"remote[- ](?:worldwide|global|anywhere)",
        r"fully\s+distributed",
        r"work\s+from\s+anywhere",
        r"(?:fully\s+)?telecommut\w+",
        r"\btelework(?:ing)?\b",
        r"home-?based\s+(?:role|position|job)",
        r"location[- ]independent",
        r"location\s*:?\s*(?:100\s*%\s*)?remote",
        r"remote\s*\(\s*(?:uk|united kingdom|eu|eea|europe|worldwide|global|emea|anywhere|"
        r"germany|france|spain|italy|netherlands|poland|portugal|romania|sweden|denmark|norway|"
        r"finland|austria|belgium|ireland|canada|us|usa)\s*\)",
        r"this\s+(?:is\s+)?(?:a\s+)?(?:full[- ]?time,\s*)?(?:fully\s+)?remote\s+(?:role|position|job|opportunity)",
        r"remote\s+(?:role|position|job|opportunity)\b"
        r"(?!\s+(?:board|site|listing|platform|portal|marketplace))",
        r"interamente\s+da\s+remot",
        r"completamente\s+da\s+remot",
        r"totalmente\s+da\s+remot",
        r"lavoro\s+da\s+remot",
        r"100\s*%\s*da\s+remot",
        r"teletrabajo\s+(?:al\s+)?100\s*%",
        r"teletrabajo\s+total",
        r"trabajo\s+100\s*%\s*remot",
        r"remoto\s+al\s+100\s*%",
        r"totalmente\s+remot",
        r"t[ée]l[ée]travail\s+total",
        r"enti[eè]rement\s+[aà]\s+distance",
        r"voll(?:st[aä]ndig|zeitig)?\s*remote",
        r"komplett\s+remote",
    ]),
    re.IGNORECASE,
)

# Remote offered as a perk or an option, not as the job's mode. Borderline on
# purpose: the model decides whether the posting is actually fully remote.
_OFFICE_PERK = re.compile(
    "|".join([
        r"smart[- ]?working",
        r"lavoro\s+agile",
        r"\bwfh\b",
        r"work\s+from\s+home",
        r"home\s*-?\s*office",
        r"remote[- ]friendly",
        r"remote\s+(?:days?|options?|working\s+allowance|possibility|opportunit)",
        r"remote\s+or\s+(?:in|hybrid|office)",
        r"hybrid\s+or\s+remote",
        r"flexible\s+(?:remote|working|work)",
        r"occasional\s+(?:remote|home)",
        r"up\s+to\s+\w+\s+days?\s+(?:remote|from\s+home|at\s+home)",
        r"mobiles?\s+arbeiten",
        r"homeoffice\s+m[oö]glich",
        r"t[ée]l[ée]travail\s+occasionnel",
    ]),
    re.IGNORECASE,
)

# Any work-mode vocabulary at all, in the five languages. Used only to decide
# whether a description with no confident phrase deserves a model call. Kept
# deliberately wide: a false positive costs one LLM batch, a false negative
# would write a remote job off as silent.
_WORK_MODE_MENTION = re.compile(
    "|".join([
        r"\bremot[eol]?\w*\b", r"da\s+remoto", r"\btele\w*lavor\w*\b", r"teletrabajo", r"trabajo\s+remoto",
        r"t[ée]l[ée]travail", r"distanciel", r"travail\s+[aà]\s+distance", r"home\s*-?\s*office",
        r"homeoffice", r"mobiles?\s+arbeiten", r"vor\s+ort", r"pr[aä]senz\w*", r"im\s+b[uü]ro",
        r"\bhybrid\w*\b", r"\bibrid\w*\b", r"h[ií]brid\w*", r"\bhybride\b", r"\bsur\s+site\b",
        r"pr[eé]sentiel", r"on-?site", r"in-?office", r"office-?based", r"in\s+ufficio",
        r"\bin\s+sede\b", r"presenza", r"presencial", r"smart[- ]?working", r"lavoro\s+agile",
        r"work\s+from\s+home", r"\bwfh\b", r"agile\s+work",
        r"telecommut\w*", r"\btelework\w*\b", r"home-?based", r"distributed\s+(?:team|workforce)",
        r"\banywhere\b",
    ]),
    re.IGNORECASE,
)

# "Remote (UK)": the parentheses name the eligible country, which is exactly the
# D3 case that must be delivered with a warning instead of rejected.
_REMOTE_PAREN_COUNTRY = re.compile(r"remote\s*\(\s*([^)]{2,24})\)", re.IGNORECASE)
# "Remote - Italy", "fully remote in Italy", "da remoto dall'Italia".
_REMOTE_ITALY = re.compile(
    r"\bremot[eo](?:\s+(?:role|position|job))?\s*(?:[-–—:,/|]\s*|\s+(?:in|from|within|anywhere\s+in|da(?:ll['’])?)\s*)"
    r"(?:italy|italia)\b",
    re.IGNORECASE)

# A negated phrase is not a policy statement ("this is not a fully remote role").
_NEGATION = re.compile(r"\b(?:not|no|non|nicht|kein\w*|pas|senza|sin)\b[\s\w]{0,18}$", re.IGNORECASE)
# "Possibility of remote work", "option to work remotely": a perk, not the mode,
# even when a confident phrase follows. Such a match is dropped and the text goes
# to the model (the mention scan sees it either way).
_PERK_PREFIX = re.compile(
    r"(?:possibilit[aà]|possibility|opzione|option|facolt[aà]|opportunit[aà]|"
    r"eligible|m[oö]glichkeit)\s+(?:di\s+|to\s+|for\s+|of\s+)?"
    r"(?:work\s+|lavorare\s+|lavoro\s+)?[\s\w]{0,12}$",
    re.IGNORECASE)
# "Fully remote or hybrid", "remote/on-site": an option, not a policy.
_PERK_SUFFIX = re.compile(
    r"^[\s,;:/-]*(?:or|and|/|und|o|ou)\s+(?:hybrid|in-?person|on-?site|office|in\s+the\s+office)\b",
    re.IGNORECASE)


def _first_confident(pattern: re.Pattern[str], text: str, *, suffix_guard: bool = False) -> str | None:
    for match in pattern.finditer(text):
        prefix = text[max(0, match.start() - 40):match.start()]
        if _NEGATION.search(prefix) or _PERK_PREFIX.search(prefix):
            continue
        if suffix_guard and _PERK_SUFFIX.search(text[match.end():match.end() + 40]):
            continue
        return match.group(0).strip()
    return None


def explicit_eligibility(text: str) -> str | None:
    """The country an explicit remote scope names, or None: "Italy" for "Remote -
    Italy" or "Remote (Italy)", the foreign countries of "Remote (UK)". Nothing
    else in a posting is read as an eligibility condition."""
    if _REMOTE_ITALY.search(text):
        return "Italy"
    for match in _REMOTE_PAREN_COUNTRY.finditer(text):
        countries = countries_in(match.group(1))
        if "Italy" in countries:
            return "Italy"
        if countries:
            return " or ".join(countries)
    return None


@dataclass(frozen=True)
class RuleVerdict:
    verdict: str
    reason: str
    advice: str = ""


def _eligibility(text: str, *, require_italy_eligibility: bool) -> tuple[str, str] | None:
    """(verdict, advice) for a job already known to be fully remote, or None when
    the tier needs Italy eligibility and the posting has no explicit scope."""
    if not require_italy_eligibility:
        return CONFIRMED, ""
    scope = explicit_eligibility(text)
    if scope == "Italy":
        return CONFIRMED, ""
    if scope:
        return ADVISED, limited_to_advice(scope)
    return None


def classify(
    description: str,
    *,
    title: str = "",
    location: str = "",
    remote_hint: str | None = None,
    require_italy_eligibility: bool = False,
    hints_enabled: bool = True,
    # reserved for Tasks 1b and 2: accepted now so the processor can pass them.
    occasional_guard: bool = True,
    cloud_hybrid_guard: bool = True,
) -> RuleVerdict | None:
    """A verdict this module is sure of, or None to let the model decide.

    `description` is the full stored text. The confident-phrase scan also reads
    the title, because "Backend MLOps Engineer - REMOTO" states the mode there.
    """
    text = f"{title}\n{description}".strip()
    lowered_location = (location or "").lower()

    if hints_enabled and remote_hint in ("hybrid", "onsite"):
        return RuleVerdict(REJECTED, f"{RULE_PREFIX}source work mode is {remote_hint}")

    hybrid = _first_confident(_CONFIDENT_HYBRID, text)
    remote = _first_confident(_CONFIDENT_REMOTE, text, suffix_guard=True)
    if not remote and re.search(r"\bremote\b", lowered_location):
        remote = "remote (location field)"

    if hybrid and not remote:
        return RuleVerdict(REJECTED, f"{RULE_PREFIX}'{hybrid}' requires on-site presence")
    if remote and not hybrid:
        eligibility = _eligibility(text, require_italy_eligibility=require_italy_eligibility)
        if eligibility is None:
            return None
        verdict, advice = eligibility
        reason = f"{RULE_PREFIX}'{remote}' states a fully remote role"
        return RuleVerdict(verdict, reason, advice)
    # Mixed signals, or a remote word only as a perk: the model decides.
    if hybrid or remote or _OFFICE_PERK.search(text) or _WORK_MODE_MENTION.search(text):
        return None
    if len(description.strip()) < _MIN_DESCRIPTION_CHARS:
        return None
    return RuleVerdict(NOT_STATED, f"{RULE_PREFIX}no work-mode phrase in the description")
