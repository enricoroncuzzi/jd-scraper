from src.tailor.generate import build_prompt, Selection, CoverLetterParts
from src.tailor.jd_source import JobDescription
from src.tailor.cv_master import load_canonical

MASTER = """# Enrico Roncuzzi

## Summary
Original summary about AI.

## Skills

**Languages:** Python

**AI stack:** LangGraph

## Experience

**AI Engineer**
  : **Hey-Movo**
  : **Jun 2026 - Present**

- First bullet about agents.

## Education
MSc

## Languages
English
"""


def _canon(tmp_path):
    p = tmp_path / "CV_master.md"
    p.write_text(MASTER)
    return load_canonical(str(p))


def _jd():
    return JobDescription(
        offer_id=1, title="AI Engineer", company="Acme", location="Remote",
        link="x", work_mode="remote", date="2026-07-02", tier=1,
        description="Build RAG agents in Python.",
    )


def test_prompt_lists_selectable_ids_and_bullets(tmp_path):
    prompt = build_prompt(_jd(), _canon(tmp_path))
    assert "exp.0.b0" in prompt
    assert "First bullet about agents." in prompt
    assert "skill.ai_stack" in prompt
    assert "Acme" in prompt
    assert "Build RAG agents in Python." in prompt


def test_prompt_forbids_rewriting_cv(tmp_path):
    low = build_prompt(_jd(), _canon(tmp_path)).lower()
    assert "may not write, rephrase, summarize, or improve any resume text" in low


def test_prompt_cover_letter_rules(tmp_path):
    low = build_prompt(_jd(), _canon(tmp_path)).lower()
    assert "60 words" in low
    assert "never use a dash" in low
    assert "based in italy" in low  # fixed close is stated as fixed


def test_prompt_includes_all_bullets_and_hr_direction(tmp_path):
    low = build_prompt(_jd(), _canon(tmp_path)).lower()
    assert "include every bullet id" in low          # never drop a bullet
    assert "written by enrico to" in low             # hr message is his outreach
    assert "hr_message" in low


def test_selection_schema_shape():
    s = Selection(
        included_bullet_ids=["exp.0.b0"],
        skill_order=["skill.ai_stack", "skill.languages"],
        cover_letter=CoverLetterParts(hook="h", bridge="b", proof_id="exp.0.b0"),
        hr_message="Hi, I saw your role.",
    )
    assert s.cover_letter.proof_id == "exp.0.b0"
    assert s.hr_message.startswith("Hi")
