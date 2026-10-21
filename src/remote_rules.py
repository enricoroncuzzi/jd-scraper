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


# --- Occasional presence ---------------------------------------------------
_PRESENCE_WORD = (r"(?:on-?site|in-?person|in\s+(?:the\s+)?office|at\s+the\s+office|"
                  r"office\s+days?|presence|visits?|trips?|travel\w*|gatherings?|"
                  r"meet-?ups?|events?)")
# Occasional presence, by construction. Deliberately NOT free-floating "quarterly"
# or "periodic": "3 days a week in the office, plus a quarterly bonus" is a
# regular hybrid sentence. "occasional"/"periodic" only count next to a presence
# word, the frequency forms are year/quarter scoped, and a bare "sprint" counts
# because the spec and three audit ids (Canonical) name team sprints. An agile
# sprint only matters when it shares a sentence with a hybrid phrase, and then
# the result is the model, never a rejection or a confirm.
_OCCASIONAL_PRESENCE = re.compile("|".join([
    rf"\boccasional(?:ly)?\s+(?:\w+\s+){{0,3}}{_PRESENCE_WORD}\b",
    rf"\b{_PRESENCE_WORD}\s+(?:\w+\s+){{0,3}}occasional(?:ly)?\b",
    rf"\bperiodic(?:ally)?\s+(?:\w+\s+){{0,3}}{_PRESENCE_WORD}\b",
    r"\bfrom\s+time\s+to\s+time\b", r"\bonce\s+in\s+a\s+while\b", r"\bnow\s+and\s+then\b",
    r"\b(?:a\s+)?few\s+times\s+(?:a|per)\s+(?:year|quarter)\b",
    r"\b(?:\w+\s+){0,3}times\s+(?:a|per)\s+(?:year|quarter)\b",
    r"\b(?:\w+\s+){0,3}times\s+(?:yearly|annually)\b",
    r"\b(?:once|twice)\s+(?:a|per)\s+(?:year|quarter)\b",
    r"\btwice[-\s](?:a\s+)?yearly\b",
    r"\b(?:bi-?annual(?:ly)?|semi-?annual(?:ly)?)\b",
    r"\bevery\s+(?:few\s+months|quarter|year)\b",
    r"\b(?:days?|weeks?|trips?|visits?)\s+(?:a|per)\s+(?:year|quarter)\b",
    r"\ba\s+(?:few|couple\s+of)\s+(?:trips?|visits?)\b",
    r"\bsprints?\b",
    r"\bonboarding\b", r"\bteam\s+events?\b", r"\boff-?sites?\b", r"\bretreats?\b",
    r"\bteam\s+meet-?ups?\b", r"\bmeet-?ups?\b", r"\bteam\s+gatherings?\b", r"\bgatherings?\b",
    r"\b(?:kick-?offs?|hackathons?|bootcamps?)\b",
    r"\bin-?person\s+team\s+days?\b", r"\bteam\s+days?\b", r"\bsite\s+visits?\b",
    r"\b(?:client|customer|team)\s+workshops?\b",
    r"\boccasionalmente\b", r"\bsaltuariament[ei]\b", r"\bogni\s+tanto\b", r"\bqualche\s+volta\b",
    r"\bocasionalmente\b", r"\bde\s+vez\s+en\s+cuando\b", r"\bespor[aá]dic\w*\b",
    r"\boccasionnellement\b", r"\bde\s+temps\s+en\s+temps\b",
    r"\bgelegentlich\w*\b", r"\bab\s+und\s+zu\b",
]), re.IGNORECASE)
# A frequency adjective directly in front of the match ("quarterly in-office
# days", "annual on-site presence").
_OCCASIONAL_QUALIFIER = re.compile(
    r"\b(?:quarterly|yearly|annual|annually|bi-?annual|semi-?annual)\s*$", re.IGNORECASE)
# "very few office-based roles" (Canonical's own wording) is not a hybrid phrase:
# it says the opposite. "a few in-office days a week" is the opposite case (a
# regular policy), so the lookbehind keeps "a few" out. Checked like a
# negation, for both guarded and unguarded scans, so the post-model hint
# override does not keep such a job rejected.
_FEW_PREFIX = re.compile(r"(?<!\ba\s)\b(?:very\s+)?few\s+$", re.IGNORECASE)
_SENTENCE_DELIMITERS = ".!?;\n\u2022"


def _sentence_around(text: str, start: int, end: int) -> str:
    left = max(text.rfind(char, 0, start) for char in _SENTENCE_DELIMITERS) + 1
    rights = [position for position in (text.find(char, end) for char in _SENTENCE_DELIMITERS)
              if position != -1]
    return text[left:min(rights) if rights else len(text)]


def occasional_presence_phrase(text: str) -> str | None:
    """The first phrase in `text` that marks presence as occasional, or None."""
    match = _OCCASIONAL_PRESENCE.search(text or "")
    return match.group(0).strip() if match else None


# "hybrid" as a technology adjective: hybrid cloud, hybrid infrastructure,
# hybrid environments. Removed before the work-mode scan, so a description whose
# only "hybrid" is technical reads as silent, while a real hybrid sentence next
# to it still rejects. Plural only, and no work/working prefix: "hybrid working
# environment" and a singular "hybrid environment" are ordinary workplace
# wording for a hybrid role and keep rejecting through _CONFIDENT_HYBRID.
_CLOUD_HYBRID = re.compile(
    r"\bhybrid[-\s]+(?:cloud|multi-?cloud|infrastructure|environments)\b", re.IGNORECASE)


def _scrub_cloud_hybrid(text: str) -> str:
    return _CLOUD_HYBRID.sub(" ", text)


def _scan_hybrid(text: str, *, occasional_guard: bool, cloud_hybrid_guard: bool,
                 few_guard: bool = True) -> tuple[str | None, str | None]:
    """(first confident hybrid phrase, first phrase suppressed as occasional presence)."""
    scanned = _scrub_cloud_hybrid(text) if cloud_hybrid_guard else text
    confident = suppressed = None
    for match in _CONFIDENT_HYBRID.finditer(scanned):
        prefix = scanned[max(0, match.start() - 40):match.start()]
        if (_NEGATION.search(prefix) or _PERK_PREFIX.search(prefix)
                or (few_guard and _FEW_PREFIX.search(prefix))):
            continue
        occasional = (
            occasional_presence_phrase(_sentence_around(scanned, match.start(), match.end()))
            or _OCCASIONAL_QUALIFIER.search(scanned[max(0, match.start() - 30):match.start()]))
        if occasional_guard and occasional:
            suppressed = suppressed or match.group(0).strip()
            continue
        confident = confident or match.group(0).strip()
    return confident, suppressed


def confident_hybrid_phrase(text: str, *, occasional_guard: bool = True,
                            cloud_hybrid_guard: bool = True, few_guard: bool = True) -> str | None:
    """The first hybrid phrase that survives the occasional-presence guard, or None."""
    return _scan_hybrid(text or "", occasional_guard=occasional_guard,
                        cloud_hybrid_guard=cloud_hybrid_guard, few_guard=few_guard)[0]


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
    scanned = _scrub_cloud_hybrid(text) if cloud_hybrid_guard else text
    lowered_location = (location or "").lower()

    if hints_enabled and remote_hint in ("hybrid", "onsite"):
        return RuleVerdict(REJECTED, f"{RULE_PREFIX}source work mode is {remote_hint}")

    hybrid, suppressed = _scan_hybrid(text, occasional_guard=occasional_guard,
                                      cloud_hybrid_guard=cloud_hybrid_guard,
                                      few_guard=occasional_guard)
    remote = _first_confident(_CONFIDENT_REMOTE, text, suffix_guard=True)
    if not remote and re.search(r"\bremote\b", lowered_location):
        remote = "remote (location field)"

    if hybrid and not remote:
        return RuleVerdict(REJECTED, f"{RULE_PREFIX}'{hybrid}' requires on-site presence")
    if remote and not hybrid and not suppressed:
        eligibility = _eligibility(text, require_italy_eligibility=require_italy_eligibility)
        if eligibility is None:
            return None
        verdict, advice = eligibility
        reason = f"{RULE_PREFIX}'{remote}' states a fully remote role"
        return RuleVerdict(verdict, reason, advice)
    if suppressed:
        # The only hybrid-looking phrase was occasional presence: the model
        # decides. This branch also covers remote + suppressed, which is NOT
        # auto-confirmed: "confirmed" is the one verdict that gets auto-tailored.
        return None
    # Mixed signals, or a remote word only as a perk: the model decides.
    if hybrid or remote or _OFFICE_PERK.search(scanned) or _WORK_MODE_MENTION.search(scanned):
        return None
    if len(description.strip()) < _MIN_DESCRIPTION_CHARS:
        return None
    return RuleVerdict(NOT_STATED, f"{RULE_PREFIX}no work-mode phrase in the description")
