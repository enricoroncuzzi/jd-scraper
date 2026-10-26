"""`CL_master.md`, the cover-letter master.

The letter is a file the owner can read and edit, next to `CV_master.md`, instead of
letterhead constants in the code: the letterhead, the letter itself with three slots the
model fills (`{{hook}}`, `{{bridge}}`, `{{proof}}`), and the Letter Proof Pool the proof slot
picks from.

The format is parsed strictly and the hand-written text passes the same rules the model's
text passes, so a broken master stops packaging with the line number rather than sending a
brace or a dash to a recruiter. The hook, bridge and proof rules themselves stay in
`build_prompt()` and `validate.py`: a text edit here must not be able to change model
behaviour without review.
"""
import re
from dataclasses import dataclass

from src.tailor.cv_master import load_canonical, parse_proof_pool_line
from src.tailor.validate import BANNED_PHRASES, has_dash

COMPANY_TOKEN = "{company}"
SLOTS = ("hook", "bridge", "proof")
LETTERHEAD_KEYS = ("name", "tagline", "contact", "title", "signature")
SECTION_NAMES = ("Letterhead", "Letter", "Proof Pool")

# A comment may not contain another opener, so an unterminated `<!--` cannot run on to a
# later `-->` and swallow a section heading: it is left in the text and reported by line.
_COMMENT_RE = re.compile(r"<!--(?:(?!<!--).)*?-->", re.DOTALL)
_LETTERHEAD_LINE_RE = re.compile(r"^-\s*(?P<key>[A-Za-z][A-Za-z ]*):\s*(?P<value>.*)$")
_SLOT_RE = re.compile(r"\{\{(?P<name>[^{}]+)\}\}")
_BRACE_RE = re.compile(r"\{([^{}]*)\}")


@dataclass(frozen=True)
class Letterhead:
    name: str
    tagline: str
    contact: tuple[str, ...]
    title: str
    signature: str


@dataclass(frozen=True)
class LetterPart:
    kind: str   # "fixed" | "hook" | "bridge" | "proof"
    text: str   # fixed paragraphs only (still holding "{company}"); "" for slots
    line: int   # 1-based line in the master file


@dataclass
class CoverLetterMaster:
    raw: str
    letterhead: Letterhead
    parts: list[LetterPart]
    proof_pool: dict[str, str]
    letterhead_lines: dict[str, int]
    proof_pool_lines: dict[str, int]


def _err(line: int, what: str) -> str:
    return f"CL master line {line}: {what}"


def _sections(lines: list[str]) -> dict[str, tuple[int, list[tuple[int, str]]]]:
    """`{name: (heading line, [(line number, raw line), ...])}`, with the required sections
    checked before any unknown or repeated heading, so a renamed section reports the useful
    message."""
    found: dict[str, tuple[int, list[tuple[int, str]]]] = {}
    current: str | None = None
    for number, line in enumerate(lines, 1):
        if line.startswith("## "):
            current = line[3:].strip()
            found[current] = (number, [])
            continue
        if current is not None:
            found[current][1].append((number, line))
    for name in SECTION_NAMES:
        if name not in found:
            raise ValueError(f"CL master: missing section '## {name}'")
    for name, (number, _) in found.items():
        if name not in SECTION_NAMES:
            raise ValueError(_err(number, f"unknown section '## {name}'"))
    seen: set[str] = set()
    for number, line in enumerate(lines, 1):
        if line.startswith("## "):
            name = line[3:].strip()
            if name in seen:
                raise ValueError(_err(number, f"duplicate section '## {name}'"))
            seen.add(name)
    return found


def _parse_letterhead(
    body: list[tuple[int, str]],
) -> tuple[Letterhead, dict[str, int]]:
    values: dict[str, str] = {}
    lines: dict[str, int] = {}
    for number, raw in body:
        stripped = raw.strip()
        if not stripped:
            continue
        match = _LETTERHEAD_LINE_RE.match(stripped)
        if not match:
            raise ValueError(_err(number, f"invalid letterhead line {stripped!r}"))
        key = match.group("key").strip().lower()
        value = match.group("value").strip()
        if key not in LETTERHEAD_KEYS:
            raise ValueError(_err(number, f"unknown letterhead key '{key}'"))
        if key in values:
            raise ValueError(_err(number, f"duplicate letterhead key '{key}'"))
        if not value:
            raise ValueError(_err(number, f"empty letterhead value for '{key}'"))
        values[key] = value
        lines[key] = number
    for key in LETTERHEAD_KEYS:
        if key not in values:
            raise ValueError(f"CL master: missing letterhead key '{key}'")
    contact = tuple(item.strip() for item in values["contact"].split("|") if item.strip())
    if not contact:
        raise ValueError(
            _err(lines["contact"], "contact needs at least one item, separated by '|'"))
    return (
        Letterhead(
            name=values["name"],
            tagline=values["tagline"],
            contact=contact,
            title=values["title"],
            signature=values["signature"],
        ),
        lines,
    )


def _paragraphs(body: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Blank-line separated paragraphs, each line stripped and the lines joined with a single
    space, with the paragraph's first line number."""
    out: list[tuple[int, str]] = []
    lines: list[str] = []
    start = 0
    for number, raw in body:
        stripped = raw.strip()
        if not stripped:
            if lines:
                out.append((start, " ".join(lines)))
                lines = []
            continue
        if not lines:
            start = number
        lines.append(stripped)
    if lines:
        out.append((start, " ".join(lines)))
    return out


def _parse_letter(heading_line: int, body: list[tuple[int, str]]) -> list[LetterPart]:
    parts: list[LetterPart] = []
    for number, para in _paragraphs(body):
        slot = _SLOT_RE.fullmatch(para)
        if slot:
            name = slot.group("name")
            if name not in SLOTS:
                raise ValueError(_err(number, f"unknown slot {{{{{name}}}}}"))
            parts.append(LetterPart(name, "", number))
            continue
        if "{{" in para or "}}" in para:
            raise ValueError(_err(number, "a slot must be alone in its paragraph"))
        for brace in _BRACE_RE.finditer(para):
            if brace.group(1) != "company":
                raise ValueError(_err(number, f"unknown brace group {{{brace.group(1)}}}"))
        residual = para.replace(COMPANY_TOKEN, "")
        if "{" in residual or "}" in residual:
            raise ValueError(
                _err(number, "unbalanced brace in letter text (only {company} may appear)"))
        parts.append(LetterPart("fixed", para, number))
    if not parts:
        raise ValueError(_err(heading_line, "no letter text"))

    found = [(part.kind, part.line) for part in parts if part.kind != "fixed"]
    seen: set[str] = set()
    for kind, number in found:
        if kind in seen:
            raise ValueError(_err(number, f"duplicate slot {{{{{kind}}}}}"))
        seen.add(kind)
    for kind in SLOTS:
        if kind not in seen:
            raise ValueError(_err(heading_line, f"missing slot {{{{{kind}}}}}"))
    for index, (kind, number) in enumerate(found):
        if kind != SLOTS[index]:
            raise ValueError(
                _err(number, f"slot {{{{{kind}}}}} comes before {{{{{SLOTS[index]}}}}}"))
    return parts


def _parse_proof_pool(body: list[tuple[int, str]]) -> tuple[dict[str, str], dict[str, int]]:
    pool: dict[str, str] = {}
    lines: dict[str, int] = {}
    for number, raw in body:
        stripped = raw.strip()
        if not stripped:
            continue
        parsed = parse_proof_pool_line(stripped)
        if parsed is None:
            raise ValueError(_err(number, f"invalid proof-pool line {stripped!r}"))
        pool_id, text = parsed
        if pool_id in pool:
            raise ValueError(_err(number, f"duplicate proof-pool id {pool_id!r}"))
        if not text:
            raise ValueError(_err(number, f"empty proof-pool sentence for {pool_id!r}"))
        if "{" in text or "}" in text:
            raise ValueError(_err(number, "braces are not allowed in proof-pool text"))
        pool[pool_id] = text
        lines[pool_id] = number
    return pool, lines


def _additional_text_checks():
    """The voice fix's first-person checker, when that change is present. Optional on
    purpose: this module must not depend on which PR merges first, and the fixed text must
    pass the same check the model's hook and bridge do once it exists."""
    from src.tailor import validate
    check = getattr(validate, "check_voice", None)
    return [check] if check else []


def parse_cl_master(text: str) -> CoverLetterMaster:
    """Parse and structurally check the master. Raises ValueError naming the line."""
    stripped_text = _COMMENT_RE.sub(
        lambda m: "\n" * m.group(0).count("\n"), text)
    marker = re.search(r"<!--|-->", stripped_text)
    if marker:
        raise ValueError(_err(
            stripped_text[:marker.start()].count("\n") + 1, "unterminated HTML comment"))

    sections = _sections(stripped_text.splitlines())
    letterhead, letterhead_lines = _parse_letterhead(sections["Letterhead"][1])
    parts = _parse_letter(sections["Letter"][0], sections["Letter"][1])
    proof_pool, proof_pool_lines = _parse_proof_pool(sections["Proof Pool"][1])
    return CoverLetterMaster(
        raw=text,
        letterhead=letterhead,
        parts=parts,
        proof_pool=proof_pool,
        letterhead_lines=letterhead_lines,
        proof_pool_lines=proof_pool_lines,
    )


def validate_cl_master(master: CoverLetterMaster) -> list[str]:
    """The content rules on the hand-written text: the dash rule and the banned phrases the
    model's text passes, plus any checker a later change adds (the voice fix). Each violation
    names its line."""
    violations: list[str] = []

    def check(text: str, line: int) -> None:
        if has_dash(text):
            violations.append(_err(line, "dash used (write with commas/periods)"))
        low = text.lower()
        for phrase in BANNED_PHRASES:
            if phrase in low:
                violations.append(_err(line, f"banned phrase: {phrase!r}"))
        for checker in _additional_text_checks():
            for reason in checker(text):
                violations.append(_err(line, reason))

    for key in LETTERHEAD_KEYS:
        value = getattr(master.letterhead, key)
        check(" | ".join(value) if key == "contact" else value, master.letterhead_lines[key])
    for part in master.parts:
        if part.kind == "fixed":
            check(part.text, part.line)
    for pool_id, text in master.proof_pool.items():
        check(text, master.proof_pool_lines[pool_id])
    return violations


def load_cl_master(path: str) -> CoverLetterMaster:
    """Read the master, parse it and apply the content rules. Raises ValueError naming the
    line, so a broken file stops packaging instead of sending its text."""
    with open(path) as f:
        raw = f.read()
    master = parse_cl_master(raw)
    violations = validate_cl_master(master)
    if violations:
        raise ValueError("\n".join(violations))
    return master
