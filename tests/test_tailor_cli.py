import os
import pytest
import tailor as tailor_cli
from src.tailor.generate import Selection, CoverLetterParts

MASTER = open("/Users/enricoroncuzzi/Desktop/raw/work/cv-source/CV_master.md").read()

NOTE = """---
date: 2026-07-02
tier: 1
work_mode: remote
company: Acme
location: Remote
link: https://x
tags: [job]
---

# AI Engineer — Acme

## Job Description

Build RAG agents. Acme builds travel tools.
"""


def _setup(tmp_path):
    master = tmp_path / "CV_master.md"
    master.write_text(MASTER)
    note = tmp_path / "acme_ai_engineer_5.md"
    note.write_text(NOTE)
    return str(master), str(note)


def _full_selection():
    from src.tailor.cv_master import load_canonical
    c = load_canonical("/Users/enricoroncuzzi/Desktop/raw/work/cv-source/CV_master.md")
    return Selection(
        included_bullet_ids=[bid for s in c.sections for bid in s.bullet_ids],
        skill_order=[sid for sid, _ in c.skills],
        cover_letter=CoverLetterParts(
            hook="I follow how Acme builds travel tools.",
            bridge="At Hey Movo I built a coordinator agent using the Model Context Protocol.",
            proof_id=c.sections[0].bullet_ids[0],
        ),
        hr_message="Hi, I saw your AI Engineer role and would love to connect.",
    )


def test_run_assembles_verbatim_and_writes_all(tmp_path, monkeypatch):
    master, note = _setup(tmp_path)
    root = tmp_path / "jd-output"
    monkeypatch.setattr(tailor_cli, "generate", lambda jd, c, key, **kw: _full_selection())
    monkeypatch.setattr(tailor_cli, "render_pdf", lambda md, css, out: open(out, "w").write("PDF"))
    monkeypatch.setattr(tailor_cli, "render_cover_letter_pdf", lambda text, out: open(out, "w").write("PDF"))
    monkeypatch.setattr(tailor_cli, "pdf_page_count", lambda path: 1)

    out_dir = tailor_cli.run(str(note), str(root), master, "", "fake")

    assert os.path.exists(os.path.join(out_dir, "Roncuzzi_CV.pdf"))
    assert os.path.exists(os.path.join(out_dir, "Roncuzzi_CL.pdf"))
    assert os.path.exists(os.path.join(out_dir, "CLAIMS_REVIEW.txt"))
    cv_md = open(os.path.join(out_dir, "Roncuzzi_CV.md")).read()
    # CV body is byte-verbatim: a known master bullet is present unchanged
    assert "Improve pipeline reliability and observability" in cv_md
    # hr_message.txt is written non-empty (a real deliverable, never emptied)
    assert open(os.path.join(out_dir, "hr_message.txt")).read().strip() != ""


def test_run_halts_on_cover_letter_violation(tmp_path, monkeypatch):
    # The CV gate cannot be tricked into a spurious halt (all bullets always assembled).
    # The realistic halt path is a genuine cover-letter violation.
    master, note = _setup(tmp_path)
    sel = _full_selection()
    sel.cover_letter.hook = "I am excited to apply and passionate about this."  # banned phrases
    monkeypatch.setattr(tailor_cli, "generate", lambda jd, c, key, **kw: sel)
    monkeypatch.setattr(tailor_cli, "render_pdf", lambda md, css, out: open(out, "w").write("PDF"))
    monkeypatch.setattr(tailor_cli, "render_cover_letter_pdf", lambda text, out: None)
    monkeypatch.setattr(tailor_cli, "pdf_page_count", lambda path: 1)

    with pytest.raises(ValueError, match="HALTED"):
        tailor_cli.run(str(note), str(tmp_path / "o"), master, "", "fake")
    # no CV pdf written on halt
    d = os.path.join(str(tmp_path / "o"), "2026-07-02", "tier1", "tailored", "acme")
    assert not os.path.exists(os.path.join(d, "Roncuzzi_CV.pdf"))
