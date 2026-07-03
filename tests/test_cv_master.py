import pytest

from src.tailor.cv_master import load_master

MASTER = """# Enrico Roncuzzi

contact line one

## Summary
Original summary sentence about AI engineering.

## Skills
**Languages:** Python, SQL

## Experience

**AI Engineer**
  : **Hey-Movo**
  : **Jun 2026 - Present**

- First bullet about agents.
- Second bullet about pipelines.

**ML Engineer Intern**
  : **ISPL**
  : **Sep 2024 - Apr 2025**

- Third bullet about datasets.

## Education
MSc

## Languages
English
"""


def _write(tmp_path):
    p = tmp_path / "CV_master.md"
    p.write_text(MASTER)
    return str(p)


def test_load_master_extracts_summary(tmp_path):
    m = load_master(_write(tmp_path))
    assert m.summary == "Original summary sentence about AI engineering."


def test_load_master_groups_bullets_by_role(tmp_path):
    m = load_master(_write(tmp_path))
    assert m.role_names == ["AI Engineer", "ML Engineer Intern"]
    assert m.bullets_by_role == [
        ["First bullet about agents.", "Second bullet about pipelines."],
        ["Third bullet about datasets."],
    ]


def test_length_contract_budgets(tmp_path):
    m = load_master(_write(tmp_path))
    lc = m.length_contract
    assert lc.summary_budget == len("Original summary sentence about AI engineering.")
    assert [r.name for r in lc.roles] == ["AI Engineer", "ML Engineer Intern"]
    assert lc.roles[0].bullet_budgets == [
        len("First bullet about agents."),
        len("Second bullet about pipelines."),
    ]


def test_reassemble_round_trips_identically(tmp_path):
    m = load_master(_write(tmp_path))
    assert m.reassemble(m.summary, m.bullets_by_role) == m.raw


def test_reassemble_substitutes_only_targets(tmp_path):
    m = load_master(_write(tmp_path))
    out = m.reassemble("NEW SUMMARY", [["A.", "B."], ["C."]])
    assert "NEW SUMMARY" in out
    assert "- A." in out and "- B." in out and "- C." in out
    assert "## Skills" in out and "## Education" in out  # untouched sections remain
    assert "First bullet about agents." not in out


def test_reassemble_raises_on_per_role_bullet_mismatch(tmp_path):
    m = load_master(_write(tmp_path))
    # master roles have [2, 1] bullets (total 3); pass [1, 2] — total matches but per-role differs
    with pytest.raises(ValueError):
        m.reassemble(m.summary, [["only one bullet"], ["first", "second"]])
