import os
import sys
import urllib.parse
from dotenv import load_dotenv
from src.tailor.jd_source import parse_note
from src.tailor.cv_master import load_master
from src.tailor.generate import generate
from src.tailor.ground_check import check_claims
from src.tailor.render_pdf import render_pdf, pdf_page_count, render_cover_letter_pdf
from src.tailor.output import artifact_dir, write_sources
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
    master = load_master(cv_master_path)
    print(f"[tailor] Tailoring for {jd.title} — {jd.company}...")

    output = generate(jd, master, api_key)
    if output is None:
        raise ValueError("generation returned no result")

    flagged = check_claims(output, master.raw, jd.description)
    if flagged:
        raise ValueError("Grounding check failed: " + "; ".join(flagged))

    bullets_by_role = [r.bullets for r in sorted(output.experience, key=lambda r: r.role_index)]
    cv_markdown = master.reassemble(output.summary, bullets_by_role)

    css = open(css_path).read() if css_path and os.path.exists(css_path) else ""
    directory = artifact_dir(jd, jd_output_root)

    cv_pdf_path = os.path.join(directory, "Roncuzzi_CV.pdf")
    render_pdf(cv_markdown, css, cv_pdf_path)
    n_pages = pdf_page_count(cv_pdf_path)
    if n_pages != 1:
        raise ValueError(
            f"tailored CV overflowed to {n_pages} pages (expected 1); rerun to regenerate"
        )
    render_cover_letter_pdf(output.cover_letter, os.path.join(directory, "Roncuzzi_CL.pdf"))
    write_sources(directory, cv_markdown, output.cover_letter, output.hr_message)

    print(f"[tailor] Done → {directory}")
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
        notify("Tailoring failed", str(e)[:120])
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
