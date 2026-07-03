from src.tailor.jd_source import parse_note, JobDescription


NOTE = """---
date: 2026-07-02
tier: 1
work_mode: remote
score: 9
company: Logicalis Spain
location: Barcelona, Spain
link: https://es.linkedin.com/jobs/view/x-4435553154
tags: [job, scraped, high-score]
---

# Data Scientist / AI Engineer — Logicalis Spain

**Location:** Barcelona, Spain
**Score:** 9/10
**Comment:** Strong fit.
**Summary:** Build RAG pipelines.
**Link:** https://es.linkedin.com/jobs/view/x-4435553154
**Scraped:** 2026-07-02

## Job Description

Create AI agents and RAG pipelines using Python and LangChain. 100% remote.
"""


def test_parse_note_extracts_fields(tmp_path):
    p = tmp_path / "logicalis_spain_data_scientist_ai_engineer_63.md"
    p.write_text(NOTE)
    jd = parse_note(str(p))
    assert isinstance(jd, JobDescription)
    assert jd.offer_id == 63
    assert jd.company == "Logicalis Spain"
    assert jd.title == "Data Scientist / AI Engineer"
    assert jd.work_mode == "remote"
    assert jd.date == "2026-07-02"
    assert jd.tier == 1
    assert jd.link == "https://es.linkedin.com/jobs/view/x-4435553154"
    assert "RAG pipelines using Python and LangChain" in jd.description


def test_parse_note_offer_id_from_filename(tmp_path):
    p = tmp_path / "acme_ai_engineer_128.md"
    p.write_text(NOTE)
    assert parse_note(str(p)).offer_id == 128
