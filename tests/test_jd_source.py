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

# Data Scientist / AI Engineer - Logicalis Spain

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


def test_parse_note_title_matches_writer_separator(tmp_path):
    from src.models import ScoredOffer
    from src.writer import _format_note, _note_filename

    offer = ScoredOffer(
        id=63,
        title="Machine Learning - LLM Platform",
        company="Acme Corp",
        location="Barcelona, Spain",
        link="https://es.linkedin.com/jobs/view/x-4435553154",
        description="Create AI agents and RAG pipelines using Python and LangChain.",
        work_mode="remote",
        score=9,
        comment="Strong fit.",
        summary="Build RAG pipelines.",
    )
    path = tmp_path / _note_filename(offer)
    path.write_text(_format_note(offer, "high-score", "2026-07-02", 1))

    jd = parse_note(str(path))
    assert jd.title == "Machine Learning - LLM Platform"
    assert jd.company == "Acme Corp"


def test_parse_note_keeps_an_em_dash_inside_the_title(tmp_path):
    note = NOTE.replace(
        "# Data Scientist / AI Engineer - Logicalis Spain",
        "# Machine Learning — LLM Platform - Acme Corp",
    )
    p = tmp_path / "acme_corp_ml_9.md"
    p.write_text(note)
    jd = parse_note(str(p))
    assert jd.title == "Machine Learning — LLM Platform"
