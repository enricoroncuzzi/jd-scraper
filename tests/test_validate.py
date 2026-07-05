from src.tailor.cv_master import load_canonical
from src.tailor.validate import (
    validate_cv, validate_cover_letter, hook_claims, REQUIRED_METRICS,
)

MASTER = """# Enrico Roncuzzi

## Summary
Shipped a MoE system at 94.1% accuracy, AUC 1.000, recovering 45 percentage points, a 1K vs 10.5M gate, a 6,000 image set.

## Skills

**Languages:** Python

## Experience

**AI Engineer**
  : **Hey-Movo**
  : **Jun 2026 - Present**

- Built agents.

**ML Engineer Intern**
  : **ISPL**
  : **Sep 2024 - Apr 2025**

- Trained detectors.

## Education
MSc

## Languages
English
"""


def _canon(tmp_path):
    p = tmp_path / "CV_master.md"
    p.write_text(MASTER)
    return load_canonical(str(p))


def test_validate_cv_passes_on_verbatim_assembly(tmp_path):
    c = _canon(tmp_path)
    b = [bid for s in c.sections for bid in s.bullet_ids]
    sk = [sid for sid, _ in c.skills]
    assert validate_cv(c.assemble(b, sk), c) == []


def test_validate_cv_flags_paraphrased_bullet(tmp_path):
    c = _canon(tmp_path)
    bad = c.raw.replace("- Built agents.", "- Built many agents and pipelines.")
    violations = validate_cv(bad, c)
    assert any("not canonical" in v.lower() for v in violations)


def test_validate_cv_flags_number_word(tmp_path):
    c = _canon(tmp_path)
    bad = c.raw.replace("94.1% accuracy", "ninety four percent accuracy")
    assert any("number word" in v.lower() for v in validate_cv(bad, c))


def test_validate_cv_flags_missing_metric(tmp_path):
    c = _canon(tmp_path)
    bad = c.raw.replace("AUC 1.000, ", "")  # drop the AUC metric
    assert any("1.000" in v for v in validate_cv(bad, c))


def test_validate_cv_flags_section_order(tmp_path):
    c = _canon(tmp_path)
    # swap the two section headers so internship precedes Hey-Movo
    bad = c.raw.replace("**AI Engineer**", "@@A@@").replace(
        "**ML Engineer Intern**", "**AI Engineer**").replace("@@A@@", "**ML Engineer Intern**")
    assert any("order" in v.lower() for v in validate_cv(bad, c))


def test_validate_cover_letter_ok():
    assert validate_cover_letter(
        "I follow how Heymondo applies AI to travel insurance.",
        "At Hey Movo I built a coordinator agent using the Model Context Protocol.",
    ) == []


def test_validate_cover_letter_banned_phrase():
    v = validate_cover_letter("I am excited to apply and passionate about travel.", "I built agents.")
    assert any("banned" in x.lower() for x in v)


def test_validate_cover_letter_too_long():
    long_hook = " ".join(["word"] * 70)
    assert any("60 words" in x for x in validate_cover_letter(long_hook, "I built one agent system."))


def test_validate_cover_letter_needs_concrete():
    # bridge with no number, system name, or named technique
    v = validate_cover_letter("I like your product.", "I would be a really good and helpful person.")
    assert any("concrete" in x.lower() for x in v)


def test_hook_claims_splits_sentences():
    claims = hook_claims("Heymondo builds travel insurance. You use AI for claims.")
    assert len(claims) == 2
