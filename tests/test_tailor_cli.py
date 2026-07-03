import os
import pytest
import tailor as tailor_cli
from src.tailor.generate import TailoredOutput, RoleBullets

MASTER = """# Enrico Roncuzzi

## Summary
Original summary about AI engineering here now.

## Skills
**Languages:** Python

## Experience

**AI Engineer**
  : **Hey-Movo**
  : **Jun 2026 - Present**

- First bullet about agents.
- Second bullet about pipelines.

## Education
MSc

## Languages
English
"""

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

Build RAG agents.
"""


def test_run_end_to_end(tmp_path, monkeypatch):
    master_path = tmp_path / "CV_master.md"
    master_path.write_text(MASTER)
    note = tmp_path / "acme_ai_engineer_5.md"
    note.write_text(NOTE)
    root = tmp_path / "jd-output"

    fake = TailoredOutput(
        summary="Tailored summary for Acme role now.",
        experience=[RoleBullets(role_index=0, bullets=["New A.", "New B."])],
        cover_letter="Dear Acme team, I am a strong fit.",
        hr_message="Hi, I saw your AI Engineer role.",
    )
    monkeypatch.setattr(tailor_cli, "generate", lambda jd, m, key, **kw: fake)
    pdfs = []
    monkeypatch.setattr(
        tailor_cli, "render_pdf",
        lambda md, css, out: pdfs.append(out) or open(out, "w").write("PDF"),
    )
    monkeypatch.setattr(tailor_cli, "pdf_page_count", lambda path: 1)

    out_dir = tailor_cli.run(
        note_path=str(note), jd_output_root=str(root),
        cv_master_path=str(master_path), css_path="", api_key="fake",
    )

    assert os.path.exists(os.path.join(out_dir, "cv.pdf"))
    assert os.path.exists(os.path.join(out_dir, "cover_letter.pdf"))
    assert os.path.exists(os.path.join(out_dir, "hr_message.txt"))
    cv_md = open(os.path.join(out_dir, "cv.md")).read()
    assert "Tailored summary for Acme role now." in cv_md
    assert "- New A." in cv_md and "- New B." in cv_md
    assert "First bullet about agents." not in cv_md  # replaced


def test_run_aborts_on_hallucinated_number(tmp_path, monkeypatch):
    master_path = tmp_path / "CV_master.md"
    master_path.write_text(MASTER)
    note = tmp_path / "acme_ai_engineer_5.md"
    note.write_text(NOTE)

    fake = TailoredOutput(
        summary="Delivered 99.9% accuracy for Acme.",
        experience=[RoleBullets(role_index=0, bullets=["New A.", "New B."])],
        cover_letter="c", hr_message="h",
    )
    monkeypatch.setattr(tailor_cli, "generate", lambda jd, m, key, **kw: fake)
    monkeypatch.setattr(tailor_cli, "render_pdf", lambda md, css, out: None)

    with pytest.raises(ValueError, match="Unsupported number"):
        tailor_cli.run(
            note_path=str(note), jd_output_root=str(tmp_path / "o"),
            cv_master_path=str(master_path), css_path="", api_key="fake",
        )


def test_run_raises_on_page_overflow(tmp_path, monkeypatch):
    master_path = tmp_path / "CV_master.md"
    master_path.write_text(MASTER)
    note = tmp_path / "acme_ai_engineer_5.md"
    note.write_text(NOTE)

    fake = TailoredOutput(
        summary="Tailored summary for Acme role now.",
        experience=[RoleBullets(role_index=0, bullets=["New A.", "New B."])],
        cover_letter="Dear Acme team, I am a strong fit.",
        hr_message="Hi, I saw your AI Engineer role.",
    )
    monkeypatch.setattr(tailor_cli, "generate", lambda jd, m, key, **kw: fake)
    monkeypatch.setattr(
        tailor_cli, "render_pdf",
        lambda md, css, out: open(out, "w").write("PDF"),
    )
    monkeypatch.setattr(tailor_cli, "pdf_page_count", lambda path: 2)

    with pytest.raises(ValueError, match="overflowed"):
        tailor_cli.run(
            note_path=str(note), jd_output_root=str(tmp_path / "o"),
            cv_master_path=str(master_path), css_path="", api_key="fake",
        )


def test_run_raises_on_none_generation(tmp_path, monkeypatch):
    master_path = tmp_path / "CV_master.md"
    master_path.write_text(MASTER)
    note = tmp_path / "acme_ai_engineer_5.md"
    note.write_text(NOTE)

    monkeypatch.setattr(tailor_cli, "generate", lambda jd, m, key, **kw: None)
    monkeypatch.setattr(tailor_cli, "render_pdf", lambda md, css, out: None)

    with pytest.raises(ValueError, match="generation returned no result"):
        tailor_cli.run(
            note_path=str(note), jd_output_root=str(tmp_path / "o"),
            cv_master_path=str(master_path), css_path="", api_key="fake",
        )
