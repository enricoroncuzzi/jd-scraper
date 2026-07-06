import os
import sys
import urllib.parse
from dotenv import load_dotenv
from src.tailor.jd_source import parse_note
from src.tailor.cv_master import load_canonical
from src.tailor.generate import generate
from src.tailor.validate import validate_cv, validate_cover_letter, hook_claims
from src.tailor.render_pdf import (
    render_pdf, pdf_page_count, render_cover_letter_pdf, compose_cover_letter,
)
from src.tailor.output import artifact_dir, write_sources, write_review
from src.tailor.notify import notify, reveal

_DEFAULT_MASTER = "/Users/enricoroncuzzi/Desktop/raw/work/cv-source/CV_master.md"
_DEFAULT_CSS = "/Users/enricoroncuzzi/Desktop/raw/work/cv-source/source/CV_css.md"
_DEFAULT_ROOT = "/Users/enricoroncuzzi/Desktop/raw/work/jd-output"


def run(
    note_path: str,
    jd_output_root: str,
    cv_master_path: str,
    css_path: str,
    api_key: str,
) -> str:
    jd = parse_note(note_path)
    canonical = load_canonical(cv_master_path)
    print(f"[tailor] Tailoring for {jd.title} at {jd.company}...")

    sel = generate(jd, canonical, api_key)
    if sel is None:
        raise ValueError("HALTED: generation returned no result")

    # Always assemble the FULL canonical bullet set (protects the one-click flow: the
    # required-metrics gate can never fail on a legit run). included_bullet_ids stays in
    # the schema for a future page-overflow feature but is NOT used to filter yet.
    all_bullets = [bid for s in canonical.sections for bid in s.bullet_ids]
    cv_markdown = canonical.assemble(all_bullets, sel.skill_order)
    cv_violations = validate_cv(cv_markdown, canonical)
    cl = sel.cover_letter
    cl_violations = validate_cover_letter(cl.hook, cl.bridge)
    claims = hook_claims(cl.hook)

    if cv_violations or cl_violations:
        directory = artifact_dir(jd, jd_output_root)
        write_review(directory, jd.company, cv_violations, cl_violations, claims)
        reason = "; ".join(cv_violations + cl_violations)
        raise ValueError(f"HALTED: {reason}")

    directory = artifact_dir(jd, jd_output_root)
    css = open(css_path).read() if css_path and os.path.exists(css_path) else ""

    cv_pdf_path = os.path.join(directory, "Roncuzzi_CV.pdf")
    render_pdf(cv_markdown, css, cv_pdf_path)
    n_pages = pdf_page_count(cv_pdf_path)
    if n_pages != 1:
        os.remove(cv_pdf_path)
        raise ValueError(f"HALTED: tailored CV overflowed to {n_pages} pages")

    cover_body = compose_cover_letter(cl.hook, cl.bridge, canonical.bullet_text(cl.proof_id))
    render_cover_letter_pdf(cover_body, os.path.join(directory, "Roncuzzi_CL.pdf"))
    write_sources(directory, cv_markdown, cover_body, sel.hr_message)
    write_review(directory, jd.company, cv_violations, cl_violations, claims)

    print(f"[tailor] Done -> {directory}")
    return directory


def resolve_uri(uri: str, jd_output_root: str) -> str:
    decoded = urllib.parse.unquote(uri)
    rel = decoded.split("tailor:", 1)[1] if "tailor:" in decoded else decoded
    rel = rel.lstrip("/")
    root = os.path.realpath(jd_output_root)
    resolved = os.path.realpath(os.path.join(root, rel))
    if resolved != root and not resolved.startswith(root + os.sep):
        raise ValueError(f"refusing note path outside jd-output root: {rel}")
    return resolved


def handle_uri(
    uri: str,
    jd_output_root: str,
    cv_master_path: str,
    css_path: str,
    api_key: str,
) -> str:
    try:
        note_path = resolve_uri(uri, jd_output_root)
        directory = run(note_path, jd_output_root, cv_master_path, css_path, api_key)
    except Exception as e:
        notify("Tailoring halted", str(e)[:150])
        raise
    notify("CV tailored", os.path.basename(directory))
    reveal(directory)
    return directory


if __name__ == "__main__":
    load_dotenv()
    args = sys.argv[1:]
    if args and args[0] == "--uri" and len(args) >= 2:
        handle_uri(args[1], _DEFAULT_ROOT, _DEFAULT_MASTER, _DEFAULT_CSS, os.environ["GEMINI_API_KEY"])
    elif args and args[0] != "--uri":
        run(args[0], _DEFAULT_ROOT, _DEFAULT_MASTER, _DEFAULT_CSS, os.environ["GEMINI_API_KEY"])
    else:
        print("Usage: python tailor.py <note_path> | --uri <tailor:...>")
        sys.exit(1)
