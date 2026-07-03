from src.tailor.generate import build_prompt, TailoredOutput, RoleBullets
from src.tailor.jd_source import JobDescription
from src.tailor.cv_master import load_master

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


def _master(tmp_path):
    p = tmp_path / "CV_master.md"
    p.write_text(MASTER)
    return load_master(str(p))


def _jd():
    return JobDescription(
        offer_id=1, title="AI Engineer", company="Acme", location="Remote",
        link="x", work_mode="remote", date="2026-07-02", tier=1,
        description="Build RAG agents in Python.",
    )


def test_prompt_contains_jd_and_budgets(tmp_path):
    prompt = build_prompt(_jd(), _master(tmp_path))
    assert "Acme" in prompt
    assert "Build RAG agents in Python." in prompt
    assert "recruiter" in prompt.lower()
    assert "never invent" in prompt.lower()
    # length budgets surfaced
    assert str(len("Original summary about AI engineering here now.")) in prompt
    assert "2 bullet" in prompt.lower() or "2 bullets" in prompt.lower()


def test_prompt_lists_the_master_bullets(tmp_path):
    prompt = build_prompt(_jd(), _master(tmp_path))
    assert "First bullet about agents." in prompt
