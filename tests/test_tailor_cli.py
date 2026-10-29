import os
import pytest
import tailor as tailor_cli
from src.tailor.cv_master import parse_proof_pool_line
from src.tailor.generate import Selection, CoverLetterParts
from tests.tailor_helpers import EXAMPLE_CL_MASTER, generation, write_cl_master

MASTER = """# Enrico Roncuzzi

contact line

## Summary
AI Engineer who shipped a MoE system at 94.1% accuracy.

## Skills

**Languages:** Python

**AI stack:** LangGraph

## Experience

**AI Engineer**
  : **Hey-Movo**
  : **Jun 2026 - Present**

- Improve pipeline reliability and observability on a live platform.

**ML Engineer Intern**
  : **ISPL**
  : **Sep 2024 - Apr 2025**

- Built a 6,000 image dataset, reaching AUC 1.000 in distribution.

- Recovered 45 percentage points with a 1K parameter gate versus a 10.5M parameter one.

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

# AI Engineer - Acme

## Job Description

Build RAG agents. Acme builds travel tools.
"""


def _setup(tmp_path):
    master = tmp_path / "CV_master.md"
    master.write_text(MASTER)
    note = tmp_path / "acme_ai_engineer_5.md"
    note.write_text(NOTE)
    return str(master), str(note), write_cl_master(tmp_path)


def _full_selection(master_path):
    from src.tailor.cv_master import load_canonical
    c = load_canonical(master_path)
    return Selection(
        included_bullet_ids=[bid for s in c.sections for bid in s.bullet_ids],
        skill_order=[sid for sid, _ in c.skills],
        cover_letter=CoverLetterParts(
            hook="I follow how Acme builds travel tools.",
            bridge="At Hey Movo I built a coordinator agent using the Model Context Protocol.",
            proof_id=c.sections[1].bullet_ids[0],
        ),
        hr_message="Hi, I saw your AI Engineer role and would love to connect.",
    )


def _fake_render_pdf(md, css, out):
    """A PDF is only the page count's input here, so write a stub and report the size the
    fit loop would have picked."""
    with open(out, "w") as f:
        f.write("PDF")
    return 14.5


def _stub_pipeline(monkeypatch, master_path, pages=lambda path: 1):
    """Everything `run` calls below the model and the browser: a fixed selection, no PDFs,
    and a one-page count unless a test says otherwise."""
    monkeypatch.setattr(tailor_cli, "generate",
                        lambda jd, c, key, **kw: generation(_full_selection(master_path)))
    monkeypatch.setattr(tailor_cli, "render_pdf", _fake_render_pdf)
    monkeypatch.setattr(tailor_cli, "render_cover_letter_pdf",
                        lambda text, out, letterhead: open(out, "w").write("PDF"))
    monkeypatch.setattr(tailor_cli, "pdf_page_count", pages)


def test_run_assembles_verbatim_and_writes_all(tmp_path, monkeypatch):
    master, note, cl = _setup(tmp_path)
    root = tmp_path / "jd-output"
    _stub_pipeline(monkeypatch, master)

    out_dir = tailor_cli.run(str(note), str(root), master, "", "fake", cl_master_path=cl)

    assert os.path.exists(os.path.join(out_dir, "Roncuzzi_CV.pdf"))
    assert os.path.exists(os.path.join(out_dir, "Roncuzzi_CL.pdf"))
    assert os.path.exists(os.path.join(out_dir, "CLAIMS_REVIEW.txt"))
    cv_md = open(os.path.join(out_dir, "Roncuzzi_CV.md")).read()
    # CV body is byte-verbatim: a known master bullet is present unchanged
    assert "Improve pipeline reliability and observability on a live platform." in cv_md
    # hr_message.txt is written non-empty (a real deliverable, never emptied)
    assert open(os.path.join(out_dir, "hr_message.txt")).read().strip() != ""


def test_run_writes_the_package_under_the_day_the_digest_names(tmp_path, monkeypatch):
    # The auto-tailoring caller passes the digest day; the note's own day is only
    # the default for the manual `tailor:` flow.
    master, note, cl = _setup(tmp_path)
    root = tmp_path / "jd-output"
    _stub_pipeline(monkeypatch, master)

    out_dir = tailor_cli.run(str(note), str(root), master, "", "fake",
                             package_day="2026-07-03", cl_master_path=cl)

    assert out_dir == os.path.join(
        str(root), "2026-07-03", "tier1", "tailored", "acme_ai_engineer_5"
    )
    assert os.path.exists(os.path.join(out_dir, "Roncuzzi_CV.pdf"))
    assert not os.path.exists(os.path.join(str(root), "2026-07-02", "tier1", "tailored"))


def _note(company: str, title: str) -> str:
    return NOTE.replace("company: Acme", f"company: {company}").replace(
        "# AI Engineer - Acme", f"# {title} - {company}"
    )


def test_failed_tailoring_does_not_overwrite_another_offer_from_the_same_company(
    tmp_path, monkeypatch,
):
    master = tmp_path / "CV_master.md"
    master.write_text(MASTER)
    cl = write_cl_master(tmp_path)
    root = tmp_path / "o"
    first = tmp_path / "alignerr_ai_tutor_101.md"
    second = tmp_path / "alignerr_ml_reviewer_202.md"
    first.write_text(_note("Alignerr", "AI Tutor"))
    second.write_text(_note("Alignerr", "ML Reviewer"))

    good = _full_selection(str(master))
    bad = _full_selection(str(master))
    bad.cover_letter.hook = "I am excited to apply and passionate about this."
    selections = iter([good, bad])
    _stub_pipeline(monkeypatch, str(master))
    monkeypatch.setattr(tailor_cli, "generate",
                        lambda jd, c, key, **kw: generation(next(selections)))

    first_dir = tailor_cli.run(str(first), str(root), str(master), "", "fake", cl_master_path=cl)
    with pytest.raises(ValueError, match="HALTED"):
        tailor_cli.run(str(second), str(root), str(master), "", "fake", cl_master_path=cl)

    second_dir = os.path.join(
        str(root), "2026-07-02", "tier1", "tailored", "alignerr_ml_reviewer_202",
    )
    assert first_dir != second_dir
    assert os.path.basename(first_dir) == "alignerr_ai_tutor_101"
    assert "PASS" in open(os.path.join(first_dir, "CLAIMS_REVIEW.txt")).read()
    assert os.path.exists(os.path.join(first_dir, "Roncuzzi_CV.pdf"))
    failed = open(os.path.join(second_dir, "CLAIMS_REVIEW.txt")).read()
    assert "FAIL" in failed
    assert "banned phrase" in failed
    assert "banned phrase" not in open(os.path.join(first_dir, "CLAIMS_REVIEW.txt")).read()


def test_run_halts_on_cover_letter_violation(tmp_path, monkeypatch):
    # The CV gate cannot be tricked into a spurious halt (all bullets always assembled).
    # The realistic halt path is a genuine cover-letter violation.
    master, note, cl = _setup(tmp_path)
    sel = _full_selection(master)
    sel.cover_letter.hook = "I am excited to apply and passionate about this."  # banned phrases
    _stub_pipeline(monkeypatch, master)
    monkeypatch.setattr(tailor_cli, "generate", lambda jd, c, key, **kw: generation(sel))
    monkeypatch.setattr(tailor_cli, "render_cover_letter_pdf", lambda text, out, letterhead: None)

    with pytest.raises(ValueError, match="HALTED"):
        tailor_cli.run(str(note), str(tmp_path / "o"), master, "", "fake", cl_master_path=cl)
    # no CV pdf written on halt
    d = os.path.join(str(tmp_path / "o"), "2026-07-02", "tier1", "tailored", "acme_ai_engineer_5")
    assert not os.path.exists(os.path.join(d, "Roncuzzi_CV.pdf"))
    assert "FAIL" in open(os.path.join(d, "CLAIMS_REVIEW.txt")).read()


def test_run_halts_on_invalid_proof_id(tmp_path, monkeypatch):
    master, note, cl = _setup(tmp_path)
    sel = _full_selection(master)
    sel.cover_letter.proof_id = "exp.9.b9"  # not a real bullet
    _stub_pipeline(monkeypatch, master)
    monkeypatch.setattr(tailor_cli, "generate", lambda jd, c, key, **kw: generation(sel))
    monkeypatch.setattr(tailor_cli, "render_cover_letter_pdf", lambda text, out, letterhead: None)
    with pytest.raises(ValueError, match="HALTED"):
        tailor_cli.run(str(note), str(tmp_path / "o"), master, "", "fake", cl_master_path=cl)
    d = os.path.join(str(tmp_path / "o"), "2026-07-02", "tier1", "tailored", "acme_ai_engineer_5")
    assert not os.path.exists(os.path.join(d, "Roncuzzi_CV.pdf"))
    assert "FAIL" in open(os.path.join(d, "CLAIMS_REVIEW.txt")).read()


def test_run_halts_on_a_third_person_cover_letter(tmp_path, monkeypatch):
    # The letter is written by Enrico as "I": a third-person hook is a letter
    # violation like a banned phrase, so it takes the same halt path.
    master, note, cl = _setup(tmp_path)
    sel = _full_selection(master)
    sel.cover_letter.hook = "Enrico builds production grade multi-agent systems for live platforms."
    _stub_pipeline(monkeypatch, master)
    monkeypatch.setattr(tailor_cli, "generate", lambda jd, c, key, **kw: generation(sel))

    with pytest.raises(ValueError, match="HALTED"):
        tailor_cli.run(str(note), str(tmp_path / "o"), master, "", "fake", cl_master_path=cl)
    d = os.path.join(str(tmp_path / "o"), "2026-07-02", "tier1", "tailored", "acme_ai_engineer_5")
    assert not os.path.exists(os.path.join(d, "Roncuzzi_CV.pdf"))
    review = open(os.path.join(d, "CLAIMS_REVIEW.txt")).read()
    assert "FAIL" in review
    assert "third person" in review.lower()


def test_run_composes_the_letter_from_the_master(tmp_path, monkeypatch):
    master, note, cl = _setup(tmp_path)
    root = tmp_path / "jd-output"
    _stub_pipeline(monkeypatch, master)
    out_dir = tailor_cli.run(str(note), str(root), master, "", "fake", cl_master_path=cl)
    letter = open(os.path.join(out_dir, "Roncuzzi_CL.md")).read()
    assert letter.startswith("Dear Acme team,\n\nI follow how Acme")
    assert letter.endswith("fits your team.")
    assert "{{" not in letter and "{company}" not in letter


def test_run_picks_the_pool_sentence_that_only_the_letter_master_has(tmp_path, monkeypatch):
    # The target state: the CV master has no pool, so lp.example.0 exists only because
    # CL_master.md carries it. eligible_proof_ids/bullet_text must see it.
    master, note, cl = _setup(tmp_path)
    root = tmp_path / "jd-output"
    _stub_pipeline(monkeypatch, master)

    def selection(jd, c, key, **kw):
        sel = _full_selection(master)
        sel.cover_letter.proof_id = "lp.example.0"
        return generation(sel)

    monkeypatch.setattr(tailor_cli, "generate", selection)
    out_dir = tailor_cli.run(str(note), str(root), master, "", "fake", cl_master_path=cl)
    letter = open(os.path.join(out_dir, "Roncuzzi_CL.md")).read()
    assert "At Example Labs, built a coordinator agent" in letter
    assert "PASS" in open(os.path.join(out_dir, "CLAIMS_REVIEW.txt")).read()
    from src.tailor.cv_master import load_canonical
    assert "lp.example.0" not in load_canonical(master).eligible_proof_ids()


def test_run_stops_when_the_letter_runs_past_one_page(tmp_path, monkeypatch):
    master, note, cl = _setup(tmp_path)
    root = tmp_path / "jd-output"
    _stub_pipeline(monkeypatch, master,
                   pages=lambda path: 1 if path.endswith("Roncuzzi_CV.pdf") else 2)
    with pytest.raises(ValueError,
                       match="HALTED: tailored cover letter overflowed to 2 pages"):
        tailor_cli.run(str(note), str(root), master, "", "fake", cl_master_path=cl)
    d = os.path.join(str(root), "2026-07-02", "tier1", "tailored", "acme_ai_engineer_5")
    # No half package: neither PDF is left behind next to a FAIL review.
    assert not os.path.exists(os.path.join(d, "Roncuzzi_CV.pdf"))
    assert not os.path.exists(os.path.join(d, "Roncuzzi_CL.pdf"))
    assert not os.path.exists(os.path.join(d, "Roncuzzi_CL.md"))
    review = open(os.path.join(d, "CLAIMS_REVIEW.txt")).read()
    assert "FAIL" in review and "cover letter overflowed to 2 pages" in review


def test_run_says_once_that_the_cv_master_still_carries_the_pool(
        tmp_path, monkeypatch, capsys):
    # The transition's deprecation row: packaging keeps working and says which file to move.
    # The two pools must be identical, so the CV master gets the fixture's exact pool line.
    master, note, cl = _setup(tmp_path)
    root = tmp_path / "jd-output"
    pool_id, pool_text = parse_proof_pool_line(
        next(line for line in EXAMPLE_CL_MASTER.splitlines() if line.startswith("- lp.")))
    with open(master, "a") as f:
        f.write(f"\n## Letter Proof Pool\n\n- {pool_id} :: {pool_text}\n")
    _stub_pipeline(monkeypatch, master)
    tailor_cli.run(str(note), str(root), master, "", "fake", cl_master_path=cl)
    out = capsys.readouterr().out
    assert out.count("still carries the Letter Proof Pool") == 1


def test_run_stops_when_the_letter_master_is_missing(tmp_path, monkeypatch):
    master, note, _cl = _setup(tmp_path)
    with pytest.raises(FileNotFoundError, match="CL_MASTER_PATH"):
        tailor_cli.run(str(note), str(tmp_path / "o"), master, "", "fake",
                       cl_master_path=str(tmp_path / "absent_CL_master.md"))
