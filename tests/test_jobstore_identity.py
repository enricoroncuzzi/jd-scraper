import pytest

from src.jobstore.identity import (
    choose_canonical, is_company_page, linkedin_job_id, normalize_company, normalize_title,
)


@pytest.mark.parametrize("raw,expected", [
    ("Acme S.r.l.", "acme"),
    ("ACME SRL", "acme"),
    ("Acme, Inc.", "acme"),
    ("Acme GmbH", "acme"),
    ("Acme Group GmbH & Co. KG", "acme group"),
    ("Leonardo S.p.A.", "leonardo"),
    ("Société Générale S.A.", "societe generale"),
    ("O'Reilly Media", "oreilly media"),
    ("AT&T", "at t"),
    ("AT & T", "at t"),
    ("  Acme   Ltd  ", "acme"),
    ("AB", "ab"),
    ("", ""),
])
def test_normalize_company(raw, expected):
    assert normalize_company(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("AI Engineer", "ai engineer"),
    ("AI/ML Engineer (m/f/d)", "ai ml engineer m f d"),
    ("Machine-Learning Engineer", "machine learning engineer"),
    ("  Data   Scientist  ", "data scientist"),
    ("Ingénieur IA", "ingenieur ia"),
])
def test_normalize_title(raw, expected):
    assert normalize_title(raw) == expected


def test_legal_suffix_only_stripped_from_the_end():
    assert normalize_company("Co Working Labs") == "co working labs"


@pytest.mark.parametrize("url,expected", [
    ("https://www.linkedin.com/jobs/view/ai-engineer-at-acme-3912345678", "3912345678"),
    ("https://it.linkedin.com/jobs/view/3912345678/", "3912345678"),
    ("https://www.linkedin.com/jobs/view/ai-engineer-at-acme-3912345678?trk=x", "3912345678"),
    ("https://example.com/jobs/3912345678", None),
    ("https://www.linkedin.com/company/acme", None),
])
def test_linkedin_job_id(url, expected):
    # The query string is ignored, so a tracking suffix never changes the id.
    assert linkedin_job_id(url) == expected


@pytest.mark.parametrize("url,expected", [
    ("https://boards.greenhouse.io/acme/jobs/123", True),
    ("https://careers.acme.com/jobs/1", True),
    ("https://www.linkedin.com/jobs/view/1", False),
    ("https://himalayas.app/companies/acme/jobs/x", False),
    ("https://it.whatjobs.com/pub_api__cpl__1", False),
    ("https://freehire.me/jobs/abc", False),
    ("https://www.jobleads.com/it/job/data-scientist--roma--e9942a", False),
    ("", False),
])
def test_is_company_page(url, expected):
    assert is_company_page(url) is expected


def test_choose_canonical_prefers_the_company_page():
    urls = ["https://www.linkedin.com/jobs/view/1", "https://careers.acme.com/jobs/1"]
    assert choose_canonical(urls) == "https://careers.acme.com/jobs/1"


def test_choose_canonical_falls_back_to_first_url():
    urls = ["https://www.linkedin.com/jobs/view/1", "https://himalayas.app/x"]
    assert choose_canonical(urls) == "https://www.linkedin.com/jobs/view/1"
