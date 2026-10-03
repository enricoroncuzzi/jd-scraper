import json
from unittest.mock import MagicMock, patch

import groq
import pytest

from src.tailor.generate import build_prompt, Selection, CoverLetterParts
from src.tailor.jd_source import JobDescription
from src.tailor.cv_master import load_canonical

MASTER = """# Enrico Roncuzzi

## Summary
Original summary about AI.

## Skills

**Languages:** Python

**AI stack:** LangGraph

## Experience

**AI Engineer**
  : **Hey-Movo**
  : **Jun 2026 - Present**

- First bullet about agents.

## Education
MSc

## Languages
English
"""


def _canon(tmp_path):
    p = tmp_path / "CV_master.md"
    p.write_text(MASTER)
    return load_canonical(str(p))


def _jd():
    return JobDescription(
        offer_id=1, title="AI Engineer", company="Acme", location="Remote",
        link="x", work_mode="remote", date="2026-07-02", tier=1,
        description="Build RAG agents in Python.",
    )


def test_prompt_lists_selectable_ids_and_bullets(tmp_path):
    prompt = build_prompt(_jd(), _canon(tmp_path))
    assert "exp.0.b0" in prompt
    assert "First bullet about agents." in prompt
    assert "skill.ai_stack" in prompt
    assert "Acme" in prompt
    assert "Build RAG agents in Python." in prompt


def test_prompt_forbids_rewriting_cv(tmp_path):
    low = build_prompt(_jd(), _canon(tmp_path)).lower()
    assert "may not write, rephrase, summarize, or improve any resume text" in low


def test_prompt_cover_letter_rules(tmp_path):
    low = build_prompt(_jd(), _canon(tmp_path)).lower()
    assert "100 words" in low
    assert "never use a dash" in low
    assert "close is fixed" in low  # the model is told not to write one
    assert "do not write a close" in low


def test_prompt_includes_all_bullets_and_hr_direction(tmp_path):
    low = build_prompt(_jd(), _canon(tmp_path)).lower()
    assert "include every bullet id" in low          # never drop a bullet
    assert "written by enrico to" in low             # hr message is his outreach
    assert "hr_message" in low


_WORK_ARRANGEMENT_WORDS = ("remote", "hybrid", "on-site", "onsite", "in-office")


def test_the_fixed_close_states_location_without_a_work_arrangement_claim():
    from src.tailor.render_pdf import compose_cover_letter

    body = compose_cover_letter(
        company="Acme",
        hook="I follow how Acme builds travel tools.",
        bridge="At Hey Movo I built a coordinator agent using the Model Context Protocol.",
        proof_text="Built the agentic layer with planner and critic loops.",
    )
    close = [p.strip() for p in body.split("\n\n") if p.strip()][-1]

    assert "based in Italy" in close
    for word in _WORK_ARRANGEMENT_WORDS:
        assert word not in close.lower()


def test_the_prompt_does_not_ask_the_model_to_assert_or_suppress_arrangement(tmp_path):
    prompt = build_prompt(_jd(), _canon(tmp_path))
    close_rule = prompt.split("The cover-letter close is fixed")[1]

    for word in _WORK_ARRANGEMENT_WORDS:
        assert word not in close_rule.lower()
    assert "relocation" not in close_rule.lower()
    assert "availability" not in close_rule.lower()


def test_selection_schema_shape():
    s = Selection(
        included_bullet_ids=["exp.0.b0"],
        skill_order=["skill.ai_stack", "skill.languages"],
        cover_letter=CoverLetterParts(hook="h", bridge="b", proof_id="exp.0.b0"),
        hr_message="Hi, I saw your role.",
    )
    assert s.cover_letter.proof_id == "exp.0.b0"
    assert s.hr_message.startswith("Hi")


def test_tailoring_generation_is_recorded(tmp_path):
    from src import telemetry
    from src.tailor.generate import generate

    response = MagicMock()
    response.model = "openai/gpt-oss-120b"
    response.usage = MagicMock(prompt_tokens=40, completion_tokens=12)
    response.choices = [MagicMock()]
    response.choices[0].message.content = json.dumps({
        "included_bullet_ids": ["exp.0.b0"],
        "skill_order": ["skill.languages", "skill.ai_stack"],
        "cover_letter": {"hook": "I focus on agents.", "bridge": "I built pipelines.", "proof_id": "exp.0.b0"},
        "hr_message": "Hi, I saw the role.",
    })
    client = MagicMock()
    client.chat.completions.create.return_value = response

    recorded = []
    real = telemetry.llm_call

    def spy(**kwargs):
        cm = real(**kwargs)
        recorded.append(kwargs)
        return cm

    with patch("groq.Groq", return_value=client), patch(
        "src.tailor.generate.telemetry.llm_call", side_effect=spy
    ):
        selection = generate(_jd(), _canon(tmp_path), api_key="k")

    assert selection.cover_letter.proof_id == "exp.0.b0"
    assert len(recorded) == 1
    assert recorded[0]["stage"] == "tailoring"
    assert recorded[0]["batch_size"] == 1
    assert recorded[0]["attempt"] == 1


def _selection_response(payload):
    response = MagicMock()
    response.model = "openai/gpt-oss-120b"
    response.usage = MagicMock(prompt_tokens=40, completion_tokens=12)
    response.choices = [MagicMock()]
    response.choices[0].message.content = json.dumps(payload)
    return response


def _valid_selection_payload():
    return {
        "included_bullet_ids": ["exp.0.b0"],
        "skill_order": ["skill.languages", "skill.ai_stack"],
        "cover_letter": {
            "hook": "I focus on agents.",
            "bridge": "I built pipelines.",
            "proof_id": "exp.0.b0",
        },
        "hr_message": "Hi, I saw the role.",
    }


def _json_validate_error():
    message = "Failed to validate JSON. json_validate_failed"
    return groq.BadRequestError(
        message,
        response=MagicMock(status_code=400, headers={}),
        body={"error": {"message": message, "code": "json_validate_failed"}},
    )


@pytest.mark.parametrize("first_result", [
    _selection_response({}),
    _json_validate_error(),
])
def test_tailoring_retries_malformed_output_once(tmp_path, first_result):
    from src.tailor.generate import generate

    client = MagicMock()
    client.chat.completions.create.side_effect = [
        first_result,
        _selection_response(_valid_selection_payload()),
    ]

    with patch("groq.Groq", return_value=client):
        selection = generate(_jd(), _canon(tmp_path), api_key="k")

    assert client.chat.completions.create.call_count == 2
    assert selection.cover_letter.proof_id == "exp.0.b0"


def test_tailoring_malformed_output_retry_is_bounded(tmp_path):
    from src.tailor.generate import generate

    client = MagicMock()
    client.chat.completions.create.side_effect = _json_validate_error()

    with patch("groq.Groq", return_value=client), pytest.raises(groq.BadRequestError):
        generate(_jd(), _canon(tmp_path), api_key="k")

    assert client.chat.completions.create.call_count == 2
