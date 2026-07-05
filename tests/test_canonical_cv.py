from src.tailor.cv_master import load_canonical

MASTER = """# Enrico Roncuzzi

contact line one

## Summary
Original summary about AI, at 94.1% accuracy.

## Skills

**Languages:** Python, SQL

**AI stack:** LangGraph, RAG

## Experience

**AI Engineer**
  : **Hey-Movo**
  : **Jun 2026 - Present**

- First bullet about agents.
- Second bullet about pipelines.
- Third bullet about tests.

**ML Engineer Intern**
  : **ISPL**
  : **Sep 2024 - Apr 2025**

- Dataset bullet.
- Detector bullet.

## Education
MSc

## Languages
English
"""


def _write(tmp_path):
    p = tmp_path / "CV_master.md"
    p.write_text(MASTER)
    return str(p)


def test_parses_skills_with_ids(tmp_path):
    c = load_canonical(_write(tmp_path))
    assert c.skills == [
        ("skill.languages", "**Languages:** Python, SQL"),
        ("skill.ai_stack", "**AI stack:** LangGraph, RAG"),
    ]


def test_parses_sections_with_bullet_ids(tmp_path):
    c = load_canonical(_write(tmp_path))
    assert [s.id for s in c.sections] == ["exp.0", "exp.1"]
    assert c.sections[0].bullet_ids == ["exp.0.b0", "exp.0.b1", "exp.0.b2"]
    assert c.bullet_text("exp.0.b0") == "First bullet about agents."
    assert c.bullet_text("exp.1.b1") == "Detector bullet."


def test_assemble_default_round_trips_to_master(tmp_path):
    c = load_canonical(_write(tmp_path))
    default_bullets = [bid for s in c.sections for bid in s.bullet_ids]
    default_skills = [sid for sid, _ in c.skills]
    assert c.assemble(default_bullets, default_skills) == c.raw


def test_assemble_reorders_skills_verbatim(tmp_path):
    c = load_canonical(_write(tmp_path))
    default_bullets = [bid for s in c.sections for bid in s.bullet_ids]
    out = c.assemble(default_bullets, ["skill.ai_stack", "skill.languages"])
    # AI stack line now appears before the Languages line, both byte-verbatim
    assert out.index("**AI stack:** LangGraph, RAG") < out.index("**Languages:** Python, SQL")
    assert "**AI stack:** LangGraph, RAG" in out and "**Languages:** Python, SQL" in out


def test_assemble_drops_a_bullet_verbatim(tmp_path):
    c = load_canonical(_write(tmp_path))
    kept = ["exp.0.b0", "exp.0.b1", "exp.0.b2", "exp.1.b0"]  # dropped exp.1.b1
    default_skills = [sid for sid, _ in c.skills]
    out = c.assemble(kept, default_skills)
    assert "- Detector bullet." not in out
    assert "- Dataset bullet." in out
    # every retained bullet is byte-verbatim
    assert "- First bullet about agents." in out
