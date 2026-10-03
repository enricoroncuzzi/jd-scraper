import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock, patch

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


def _selection():
    return Selection(
        included_bullet_ids=["exp.0.b0"],
        skill_order=["skill.languages", "skill.ai_stack"],
        cover_letter=CoverLetterParts(
            hook="I focus on agents.", bridge="I built pipelines.", proof_id="exp.0.b0",
        ),
        hr_message="Hi, I saw the role.",
    )


def test_tailoring_generation_is_recorded(tmp_path):
    from src import telemetry
    from src.tailor.generate import generate

    chain = MagicMock()
    chain.invoke.return_value = _selection()
    recorded = []
    real = telemetry.llm_call

    def spy(**kwargs):
        cm = real(**kwargs)
        recorded.append(kwargs)
        return cm

    with patch("src.tailor.generate._build_chain", return_value=chain), patch(
        "src.tailor.generate.telemetry.llm_call", side_effect=spy
    ):
        selection = generate(_jd(), _canon(tmp_path), api_key="k")

    assert selection.cover_letter.proof_id == "exp.0.b0"
    assert len(recorded) == 1
    assert recorded[0]["stage"] == "tailoring"
    assert recorded[0]["provider"] == "openrouter"
    assert recorded[0]["request_model"] == "qwen/qwen3.8-27b:free"
    assert recorded[0]["batch_size"] == 1
    assert recorded[0]["attempt"] == 1


def test_tailoring_retries_empty_structured_output_once(tmp_path):
    from src.tailor.generate import generate

    chain = MagicMock()
    chain.invoke.side_effect = [None, _selection()]

    with patch("src.tailor.generate._build_chain", return_value=chain):
        selection = generate(_jd(), _canon(tmp_path), api_key="k")

    assert chain.invoke.call_count == 2
    assert selection.cover_letter.proof_id == "exp.0.b0"


def test_tailoring_empty_structured_output_retry_is_bounded(tmp_path):
    from src.scorer import _EmptyStructuredOutput
    from src.tailor.generate import generate

    chain = MagicMock()
    chain.invoke.return_value = None

    with patch("src.tailor.generate._build_chain", return_value=chain), pytest.raises(
        _EmptyStructuredOutput
    ):
        generate(_jd(), _canon(tmp_path), api_key="k")

    assert chain.invoke.call_count == 2


def test_tailoring_request_routes_qwen_with_structured_fallbacks(tmp_path, monkeypatch):
    from src.tailor.generate import generate

    captured = []

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            captured.append(body)
            tool_name = body["tools"][0]["function"]["name"]
            payload = {
                "id": "gen-1",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [{
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps({
                                    "included_bullet_ids": ["exp.0.b0"],
                                    "skill_order": ["skill.languages", "skill.ai_stack"],
                                    "cover_letter": {
                                        "hook": "I focus on agents.",
                                        "bridge": "I built pipelines.",
                                        "proof_id": "exp.0.b0",
                                    },
                                    "hr_message": "Hi, I saw the role.",
                                }),
                            },
                        }],
                    },
                }],
                "usage": {"prompt_tokens": 40, "completion_tokens": 12, "total_tokens": 52},
            }
            raw = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(
        "src.tailor.generate._OPENROUTER_BASE_URL",
        f"http://127.0.0.1:{server.server_port}/api/v1",
    )
    try:
        selection = generate(_jd(), _canon(tmp_path), api_key="k")
    finally:
        server.shutdown()

    assert selection.cover_letter.proof_id == "exp.0.b0"
    request = captured[0]
    assert request["model"] == "qwen/qwen3.8-27b:free"
    assert request["models"] == [
        "nvidia/nemotron-3-super-120b-a12b:free",
        "dots-studio/dots-3-note-preview:free",
        "liquid/lfm-2.5-2.6b:free",
    ]
    assert request["tool_choice"]["function"]["name"] == request["tools"][0]["function"]["name"]
