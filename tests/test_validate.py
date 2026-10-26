from src.tailor.cv_master import load_canonical
from src.tailor.validate import (
    validate_cv, validate_cover_letter, hook_claims, check_voice, REQUIRED_METRICS,
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
        "Heymondo",
    ) == []


def test_validate_cover_letter_banned_phrase():
    v = validate_cover_letter(
        "I am excited to apply and passionate about travel.", "I built agents.", "Acme"
    )
    assert any("banned" in x.lower() for x in v)


def test_validate_cover_letter_too_long():
    long_hook = " ".join(["word"] * 110)
    assert any(
        "100 words" in x
        for x in validate_cover_letter(long_hook, "I built one agent system.", "Acme")
    )


def test_validate_cover_letter_needs_concrete():
    # bridge with no number, system name, or named technique
    v = validate_cover_letter(
        "I like your product.", "I would be a really good and helpful person.", "Acme"
    )
    assert any("concrete" in x.lower() for x in v)


def test_validate_cover_letter_rejects_company_first_opener():
    v = validate_cover_letter(
        "Acme is building production-grade GenAI systems for enterprise clients.",
        "I built a coordinator agent using the Model Context Protocol.",
        "Acme",
    )
    opener = [x for x in v if "mail-merge" in x.lower()]
    assert len(opener) == 1
    assert "open with your own interest or capability first" in opener[0]
    assert "enrico" not in opener[0].lower()


def test_hook_claims_splits_sentences():
    claims = hook_claims("Heymondo builds travel insurance. You use AI for claims.")
    assert len(claims) == 2


def test_check_voice_passes_a_first_person_hook_and_bridge():
    assert check_voice(
        "I focus on building reliable LLM pipelines with rigorous evaluation. "
        "At Hey Movo I built a coordinator agent using the Model Context Protocol."
    ) == []


def test_check_voice_flags_a_hook_that_names_the_owner():
    assert check_voice(
        "Enrico focuses on building reliable LLM pipelines. I want to bring that "
        "to your evals work."
    ) == ["Enrico focuses on building reliable LLM pipelines."]


def test_check_voice_flags_the_owner_surname_alone():
    assert check_voice(
        "Roncuzzi builds production-grade multi-agent systems."
    ) == ["Roncuzzi builds production-grade multi-agent systems."]


def test_check_voice_flags_a_third_person_possessive_bridge():
    assert check_voice(
        "His work on coordinator agents transfers directly to your evals need."
    ) == ["His work on coordinator agents transfers directly to your evals need."]


def test_check_voice_flags_he_and_him():
    text = (
        "The agent he built ships tools over the Model Context Protocol. "
        "Acme needs exactly that, so I am writing to him."
    )
    assert check_voice(text) == [
        "The agent he built ships tools over the Model Context Protocol.",
        "Acme needs exactly that, so I am writing to him.",
    ]


def test_check_voice_flags_his_about_another_person_the_prompt_forbids():
    hook = "I want to work on the evals stack your CTO Marco describes in his posts."
    assert check_voice(hook) == [hook]


def test_check_voice_passes_the_same_hook_rewritten_by_role():
    assert check_voice(
        "I want to work on the evals stack your CTO Marco describes in the posts."
    ) == []
    assert validate_cover_letter(
        "I want to work on the evals work your CTO describes.",
        "At Hey Movo I built a coordinator agent using the Model Context Protocol.",
        "Acme",
    ) == []


def test_check_voice_is_case_insensitive():
    assert check_voice("ENRICO builds production agent systems.") == [
        "ENRICO builds production agent systems."
    ]


def test_check_voice_matches_whole_words_only():
    # "the", "other", "their" and "where" all contain "he" as a substring
    assert check_voice(
        "The company's other teams build where their own models run."
    ) == []


def test_check_voice_allows_third_person_about_the_company_or_others():
    assert check_voice(
        "Their engineering team ships weekly and the founders keep their own evals. "
        "I want to join that team."
    ) == []


def test_validate_cover_letter_flags_a_third_person_hook():
    v = validate_cover_letter(
        "Enrico builds production-grade multi-agent systems for live platforms.",
        "At Hey Movo I built a coordinator agent using the Model Context Protocol.",
        "Acme",
    )
    assert any("third person" in x.lower() for x in v)


def test_validate_cover_letter_flags_a_third_person_bridge():
    v = validate_cover_letter(
        "I follow how Acme applies AI to travel insurance.",
        "His work on LLM pipelines transfers to their evals need with MCP.",
        "Acme",
    )
    assert any("third person" in x.lower() for x in v)


def test_validate_cover_letter_passes_when_voice_is_first_person():
    v = validate_cover_letter(
        "I follow how Acme applies AI to travel insurance.",
        "At Hey Movo I built a coordinator agent using the Model Context Protocol.",
        "Acme",
    )
    assert not any("third person" in x.lower() for x in v)
