import re
from src.tailor.cv_master import CanonicalCV

REQUIRED_METRICS = ["94.1%", "1.000", "45 percentage points", "1K", "10.5M", "6,000"]

BANNED_PHRASES = [
    "excited to", "passionate about", "leverage", "cutting-edge", "delve",
    "seamless", "fast-paced", "align with your mission", "thrilled",
    "furthermore", "moreover", "tapestry", "resonates",
    "the intersection of", "i am writing to",
]

_NUM_WORDS = (
    "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twenty|"
    "thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million"
)
_NUM_WORD_RE = re.compile(
    rf"\b({_NUM_WORDS})\b[\s\w]{{0,20}}?\b(percent|points?|parameter|accuracy|auc)\b",
    re.IGNORECASE,
)
# a concrete specific = a digit OR a known system/technique name
_CONCRETE_RE = re.compile(
    r"\d|Model Context Protocol|MCP|LangGraph|FastAPI|ResNet|Mixture of Experts|MoE|"
    r"PyTorch|VAE|RAG|Gradio|Docker|coordinator agent",
    re.IGNORECASE,
)


def _cv_bullets(md: str) -> list[str]:
    m = re.search(r"## Experience\n(.*?)(?=\n## |\Z)", md, re.DOTALL)
    exp = m.group(1) if m else ""
    return [ln.strip()[2:] for ln in exp.splitlines() if ln.strip().startswith("- ")]


def _cv_summary(md: str) -> str:
    m = re.search(r"## Summary\n(.*?)(?=\n## |\Z)", md, re.DOTALL)
    return m.group(1).strip() if m else ""


def _section_order(md: str) -> list[str]:
    m = re.search(r"## Experience\n(.*?)(?=\n## |\Z)", md, re.DOTALL)
    exp = m.group(1) if m else ""
    names = []
    for line in exp.splitlines():
        s = line.strip()
        if re.match(r"^\*\*.+\*\*$", s) and not line.startswith(" "):
            names.append(s.strip("*").strip())
    return names


def validate_cv(assembled_md: str, canonical: CanonicalCV) -> list[str]:
    violations: list[str] = []
    # rule 1: every CV-body bullet + the summary must be canonical (byte match)
    known_bullets = canonical.all_bullet_texts()
    for b in _cv_bullets(assembled_md):
        if b not in known_bullets:
            violations.append(f"CV bullet not canonical (paraphrased?): {b!r}")
    if _cv_summary(assembled_md) != canonical.summary:
        violations.append("CV summary not canonical (paraphrased?)")
    # rule 2: number-word next to a metric noun
    if _NUM_WORD_RE.search(assembled_md):
        violations.append("number word used next to a metric (should be digits)")
    # rule 3: required metrics present
    for metric in REQUIRED_METRICS:
        if metric not in assembled_md:
            violations.append(f"required metric missing: {metric}")
    # rule 4: reverse-chronological section order = canonical order
    if _section_order(assembled_md) != [s.name for s in canonical.sections]:
        violations.append("experience section order is not reverse-chronological")
    return violations


def validate_cover_letter(hook: str, bridge: str) -> list[str]:
    violations: list[str] = []
    combined = f"{hook} {bridge}"
    low = combined.lower()
    for phrase in BANNED_PHRASES:
        if phrase in low:
            violations.append(f"banned phrase: {phrase!r}")
    if "—" in combined or "–" in combined or " - " in combined:
        violations.append("dash used (write with commas/periods)")
    words = len(combined.split())
    if words > 60:
        violations.append(f"hook+bridge is {words} words (max 60 words)")
    if not _CONCRETE_RE.search(bridge):
        violations.append("bridge has no concrete specific (need a number, system, or technique)")
    return violations


def hook_claims(hook: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", hook.strip()) if s.strip()]
