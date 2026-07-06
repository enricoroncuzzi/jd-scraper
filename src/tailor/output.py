import os
from src.writer import _slugify
from src.tailor.jd_source import JobDescription


def artifact_dir(jd: JobDescription, jd_output_root: str) -> str:
    directory = os.path.join(
        jd_output_root, jd.date, f"tier{jd.tier}", "tailored", _slugify(jd.company)
    )
    os.makedirs(directory, exist_ok=True)
    return directory


def write_sources(
    directory: str, cv_markdown: str, cover_letter: str, hr_message: str
) -> None:
    with open(os.path.join(directory, "Roncuzzi_CV.md"), "w") as f:
        f.write(cv_markdown)
    with open(os.path.join(directory, "Roncuzzi_CL.md"), "w") as f:
        f.write(cover_letter)
    with open(os.path.join(directory, "hr_message.txt"), "w") as f:
        f.write(hr_message)


def write_review(
    directory: str,
    company: str,
    cv_violations: list[str],
    cl_violations: list[str],
    claims: list[str],
) -> None:
    status = "PASS" if not cv_violations and not cl_violations else "FAIL"
    lines = [f"Send-readiness review — {company}", f"Overall: {status}", ""]
    lines.append("CV gate: " + ("PASS" if not cv_violations else "FAIL"))
    lines += [f"  - {v}" for v in cv_violations]
    lines.append("Cover-letter checks: " + ("PASS" if not cl_violations else "FAIL"))
    lines += [f"  - {v}" for v in cl_violations]
    lines += ["", "Company claims in the hook (confirm each is true before sending):"]
    lines += [f"  [ ] {c}" for c in claims] or ["  (none)"]
    with open(os.path.join(directory, "CLAIMS_REVIEW.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")
