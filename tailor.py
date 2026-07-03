import os
import sys
from dotenv import load_dotenv
from src.tailor.jd_source import parse_note
from src.tailor.cv_master import load_master
from src.tailor.generate import generate
from src.tailor.ground_check import check_claims
from src.tailor.render_pdf import render_pdf, pdf_page_count
from src.tailor.output import artifact_dir, write_sources

_DEFAULT_MASTER = "/Users/enricoroncuzzi/Desktop/raw/work/cv-source/CV_master.md"
_DEFAULT_CSS = "/Users/enricoroncuzzi/Desktop/raw/work/cv-source/cv_css.md"
_DEFAULT_ROOT = "/Users/enricoroncuzzi/Desktop/raw/work/jd-output"

_COVER_CSS = "#vue-smart-pages-preview { font-family: Georgia, serif; line-height: 1.5; }"


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

    cv_pdf_path = os.path.join(directory, "cv.pdf")
    render_pdf(cv_markdown, css, cv_pdf_path)
    n_pages = pdf_page_count(cv_pdf_path)
    if n_pages != 1:
        raise ValueError(
            f"tailored CV overflowed to {n_pages} pages (expected 1); rerun to regenerate"
        )
    render_pdf(output.cover_letter, _COVER_CSS, os.path.join(directory, "cover_letter.pdf"))
    write_sources(directory, cv_markdown, output.cover_letter, output.hr_message)

    print(f"[tailor] Done → {directory}")
    return directory


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python tailor.py <note_path>")
        sys.exit(1)
    load_dotenv()
    run(
        note_path=sys.argv[1],
        jd_output_root=_DEFAULT_ROOT,
        cv_master_path=_DEFAULT_MASTER,
        css_path=_DEFAULT_CSS,
        api_key=os.environ["GEMINI_API_KEY"],
    )
