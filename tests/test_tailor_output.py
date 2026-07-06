import os
from src.tailor.output import artifact_dir, write_sources
from src.tailor.jd_source import JobDescription


def _jd(company="Logicalis Spain"):
    return JobDescription(
        offer_id=63, title="AI Engineer", company=company, location="ES",
        link="x", work_mode="remote", date="2026-07-02", tier=1,
        description="d",
    )


def test_artifact_dir_path_and_creation(tmp_path):
    d = artifact_dir(_jd(), str(tmp_path))
    assert d == os.path.join(
        str(tmp_path), "2026-07-02", "tier1", "tailored", "logicalis_spain"
    )
    assert os.path.isdir(d)


def test_write_sources_creates_three_files(tmp_path):
    d = artifact_dir(_jd(), str(tmp_path))
    write_sources(d, "# CV", "Dear team", "Hi there")
    assert (open(os.path.join(d, "Roncuzzi_CV.md")).read()) == "# CV"
    assert (open(os.path.join(d, "Roncuzzi_CL.md")).read()) == "Dear team"
    assert (open(os.path.join(d, "hr_message.txt")).read()) == "Hi there"


def test_write_review_lists_claims_and_status(tmp_path):
    from src.tailor.output import write_review
    d = str(tmp_path)
    write_review(d, "Acme", cv_violations=[], cl_violations=[],
                 claims=["Acme builds travel tools.", "You use AI for support."])
    text = open(os.path.join(d, "CLAIMS_REVIEW.txt")).read()
    assert "Acme" in text
    assert "PASS" in text  # no violations
    assert "Acme builds travel tools." in text
    assert "You use AI for support." in text


def test_write_review_shows_failures(tmp_path):
    from src.tailor.output import write_review
    d = str(tmp_path)
    write_review(d, "Acme", cv_violations=["required metric missing: 94.1%"],
                 cl_violations=[], claims=[])
    text = open(os.path.join(d, "CLAIMS_REVIEW.txt")).read()
    assert "FAIL" in text
    assert "required metric missing: 94.1%" in text
