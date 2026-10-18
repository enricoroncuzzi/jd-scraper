"""Check the new rendering against a real day folder written by the old pipeline.

    python -m src.delivery.golden_check ~/Desktop/raw/work/jd-output/2026-10-12

Every note in the folder is rebuilt into a delivery job and rendered again; the text
must match the original except for the two added frontmatter fields. Each tier's
digest is rebuilt from its notes and compared line by line (as a multiset: the order of
equal scores in the original depends on scoring order, which notes do not record).
"""
import json
import os
import re
import sys
from datetime import date

from src.delivery.jobs import DeliveryJob
from src.delivery.render import render_digest, render_note

VERIFICATION = {1: True, 2: False, 3: True, 4: True}
_ADDED = re.compile(r"^(source|also_seen_on|remote_advice): .*\n", re.MULTILINE)
_DEFERRED = re.compile(r"(\d+) offer\(s\) (?:are )?still unscored")


def note_to_job(text: str, filename: str) -> DeliveryJob:
    front = dict(line.split(": ", 1) for line in re.search(r"^---\n(.*?)\n---", text, re.DOTALL)
                 .group(1).splitlines() if ": " in line)
    body = text.split("\n---\n", 1)[1]
    company = front["company"]
    heading = re.search(r"^# (.+)$", body, re.MULTILINE).group(1)
    field = lambda name: re.search(rf"^\*\*{name}:\*\* ?(.*)$", body, re.MULTILINE).group(1)  # noqa: E731
    warning = re.search(r"^⚠ description: (\w+)$", body, re.MULTILINE)
    description = text.split("## Job Description\n\n", 1)[1]
    return DeliveryJob(
        id=filename, note_no=int(re.search(r"_(\d+)\.md$", filename).group(1)),
        title=heading.removesuffix(f" - {company}"), company=company, location_raw=front["location"],
        canonical_url=front["link"], description=description[:-1] if description.endswith("\n") else description,
        description_status=warning.group(1) if warning else "ok", work_mode=front["work_mode"],
        remote_verdict=front["remote_verdict"], remote_reason=json.loads(front["remote_reason"]),
        score=int(front["score"]), comment=field("Comment"), summary=field("Summary"),
        tier=int(front["tier"]), note_day=None, alerted_at=None)


def check_day(day_dir: str, threshold: int = 8) -> list[str]:
    """Differences between the old files and a fresh render. Empty means identical."""
    problems = []
    day = date.fromisoformat(os.path.basename(day_dir.rstrip("/")))
    for tier in sorted(VERIFICATION):
        tier_dir = os.path.join(day_dir, f"tier{tier}")
        scraped = os.path.join(tier_dir, "scraped")
        jobs = []
        for name in sorted(os.listdir(scraped)) if os.path.isdir(scraped) else []:
            with open(os.path.join(scraped, name), encoding="utf-8") as handle:
                original = handle.read()
            try:
                job = note_to_job(original, name)
            except (AttributeError, KeyError, ValueError) as exc:
                problems.append(f"tier{tier}/{name}: cannot rebuild ({type(exc).__name__}: {exc})")
                continue
            jobs.append(job)
            fresh = _ADDED.sub("", render_note(job, threshold, date.fromisoformat(original.split("date: ", 1)[1][:10])))
            if fresh != original:
                first = next((i for i, (a, b) in enumerate(zip(fresh.splitlines(), original.splitlines())) if a != b), 0)
                problems.append(f"tier{tier}/{name}: differs near line {first + 1}")
        digest_path = os.path.join(tier_dir, "digest.md")
        if os.path.exists(digest_path):
            with open(digest_path, encoding="utf-8") as handle:
                original = handle.read()
            deferred = _DEFERRED.search(original)
            fresh = render_digest(jobs, day, threshold, tier, VERIFICATION[tier],
                                  int(deferred.group(1)) if deferred else 0)
            if sorted(fresh.splitlines()) != sorted(original.splitlines()):
                problems.append(f"tier{tier}/digest.md: lines differ")
    return problems


def main(argv: list[str]) -> int:
    problems = check_day(argv[0])
    print("\n".join(problems) if problems else "identical: every note and digest matches the old format")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
