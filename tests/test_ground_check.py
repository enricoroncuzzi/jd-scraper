from src.tailor.ground_check import check_claims
from src.tailor.generate import TailoredOutput, RoleBullets

MASTER = "Reached 94.1% accuracy on 6,000 images across 5 detectors."


def _out(summary="", bullets=None, cover="", hr=""):
    return TailoredOutput(
        summary=summary,
        experience=[RoleBullets(role_index=0, bullets=bullets or [])],
        cover_letter=cover,
        hr_message=hr,
    )


def test_clean_when_numbers_match_master():
    out = _out(summary="Hit 94.1% on 6,000 images.", bullets=["Trained 5 detectors."])
    assert check_claims(out, MASTER) == []


def test_flags_fabricated_number():
    out = _out(summary="Hit 99.9% accuracy.", bullets=["Led a team of 12."])
    flagged = check_claims(out, MASTER)
    assert any("99.9" in f for f in flagged)
    assert any("12" in f for f in flagged)


def test_ignores_numbers_in_hr_message():
    # hr_message is outreach copy, not a CV claim — not ground-checked
    out = _out(summary="Hit 94.1%.", hr="I have 3 reasons to apply.")
    assert check_claims(out, MASTER) == []


def test_cover_letter_number_grounded_in_jd_not_flagged():
    jd_text = "This role is 100% remote."
    out = _out(summary="Hit 94.1% on 6,000 images.", cover="Excited about the 100% remote setup.")
    assert check_claims(out, MASTER, jd_text) == []


def test_summary_number_only_in_jd_still_flagged():
    jd_text = "This role is 100% remote."
    out = _out(summary="This is a 100% remote role for me.")
    flagged = check_claims(out, MASTER, jd_text)
    assert any("100" in f for f in flagged)


def test_normalizes_thousands_separators_and_trailing_zeros():
    out = _out(summary="Hit 94.10% on 6000 images.")
    assert check_claims(out, MASTER) == []
