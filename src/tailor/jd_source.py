import os
import re
from pydantic import BaseModel


class JobDescription(BaseModel):
    offer_id: int
    title: str
    company: str
    location: str = "N/A"
    link: str = ""
    work_mode: str = ""
    date: str = ""
    tier: int = 0
    description: str = ""


def _frontmatter(text: str) -> dict:
    m = re.search(r"^---\n(.*?)\n---", text, re.DOTALL)
    fields: dict = {}
    if not m:
        return fields
    for line in m.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


def _title(text: str) -> str:
    m = re.search(r"^# (.+)$", text, re.MULTILINE)
    if not m:
        return ""
    heading = m.group(1).strip()
    return heading.rsplit(" — ", 1)[0].strip() if " — " in heading else heading


def _description(text: str) -> str:
    m = re.search(r"## Job Description\n\n(.*)$", text, re.DOTALL)
    return m.group(1).strip() if m else ""


def _offer_id(note_path: str) -> int:
    stem = os.path.splitext(os.path.basename(note_path))[0]
    m = re.search(r"_(\d+)$", stem)
    return int(m.group(1)) if m else -1


def parse_note(note_path: str) -> JobDescription:
    with open(note_path) as f:
        text = f.read()
    fm = _frontmatter(text)
    return JobDescription(
        offer_id=_offer_id(note_path),
        title=_title(text),
        company=fm.get("company", "N/A"),
        location=fm.get("location", "N/A"),
        link=fm.get("link", ""),
        work_mode=fm.get("work_mode", ""),
        date=fm.get("date", ""),
        tier=int(fm.get("tier", "0") or "0"),
        description=_description(text),
    )
