import hashlib
import json
import re
from datetime import datetime, timezone

import pytest

from src.tailor.cl_master import parse_cl_master
from src.tailor.generate import CoverLetterParts, Selection
from src.tailor.trace import (
    TRACE_FILENAME,
    build_trace,
    file_sha256,
    letter_parts,
    write_trace,
)
from tests.tailor_helpers import EXAMPLE_CL_MASTER, generation


def _master():
    return parse_cl_master(EXAMPLE_CL_MASTER)


def _selection():
    return Selection(
        included_bullet_ids=["exp.0.b0"],
        skill_order=["skill.ai_stack", "skill.languages"],
        cover_letter=CoverLetterParts(
            hook="I follow how Acme builds travel tools.",
            bridge="At Hey Movo I built a coordinator agent using the Model Context Protocol.",
            proof_id="lp.example.0",
        ),
        hr_message="Hi, I saw the role.",
    )


def _trace(tmp_path, **overrides):
    """A full trace over synthetic files, with any field a test cares about overridden."""
    cv = tmp_path / "CV_master.md"
    cv.write_text("# Alex Doe\n")
    cl = tmp_path / "CL_master.md"
    cl.write_text(EXAMPLE_CL_MASTER)
    fields = dict(
        origin="auto",
        status="PASS",
        stop=None,
        cv_master_path=str(cv),
        cl_master_path=str(cl),
        css_path="",
        prompt_version="0123456789ab",
        generation=generation(_selection()),
        skill_order=["skill.ai_stack", "skill.languages"],
        fit_px=14.5,
        letter=letter_parts(_master(), "Acme", "lp.example.0"),
    )
    fields.update(overrides)
    return build_trace(**fields)


def test_file_sha256_hashes_bytes_and_is_none_for_a_missing_or_empty_path(tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes(b"abc")
    assert file_sha256(str(path)) == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
    assert file_sha256("") is None
    assert file_sha256(str(tmp_path / "nope")) is None


def test_letter_parts_fill_the_company_and_mirror_compose():
    master = _master()
    parts = letter_parts(master, " Acme ", "lp.example.0")

    assert [p["kind"] for p in parts] == ["fixed", "hook", "bridge", "proof", "fixed"]
    assert parts[0] == {"kind": "fixed", "text": "Dear Acme team,"}
    assert parts[3] == {"kind": "proof", "proof_id": "lp.example.0"}
    # One trace entry per paragraph of the letter compose() would actually write.
    assert len(parts) == len(master.compose("Acme", "h.", "b.", "p.").split("\n\n"))


def test_build_trace_has_exactly_the_documented_keys_in_order(tmp_path):
    trace = _trace(tmp_path)
    assert list(trace) == [
        "schema", "made_at", "origin", "status", "stop", "cv_master_sha256",
        "cl_master_sha256", "css_sha256", "prompt_version", "model_requested",
        "model_answered", "fit_px", "selection", "letter_parts",
    ]
    assert list(trace["selection"]) == ["skill_order", "proof_id", "hook", "bridge"]
    assert trace["schema"] == 1


def test_build_trace_records_made_at_with_the_local_offset(tmp_path):
    trace = _trace(tmp_path, now=datetime(2026, 7, 2, 7, 30, tzinfo=timezone.utc))
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d", trace["made_at"])


def test_build_trace_keeps_an_unknown_answering_model_as_null(tmp_path):
    trace = _trace(tmp_path, generation=generation(_selection(), answered=None))
    assert trace["model_answered"] is None
    assert trace["model_requested"] == "nvidia/nemotron-3-ultra-550b-a55b:free"


def test_build_trace_css_hash_is_null_without_a_css_file(tmp_path):
    assert _trace(tmp_path, css_path="")["css_sha256"] is None
    assert _trace(tmp_path, css_path=str(tmp_path / "absent.css"))["css_sha256"] is None


def test_build_trace_refuses_an_unknown_origin_or_status(tmp_path):
    with pytest.raises(ValueError):
        _trace(tmp_path, origin="server")
    with pytest.raises(ValueError):
        _trace(tmp_path, status="OK")


def test_the_trace_holds_no_path_and_no_contact_detail(tmp_path):
    trace = _trace(tmp_path)
    blob = json.dumps(trace)
    assert str(tmp_path) not in blob
    assert "alex.doe@example.com" not in blob
    assert "000 000 0000" not in blob
    assert "Alex Doe" not in blob


def test_write_trace_is_atomic_and_round_trips(tmp_path):
    trace = _trace(tmp_path)
    path = write_trace(str(tmp_path), trace)

    assert path.endswith(TRACE_FILENAME)
    assert json.load(open(path)) == trace
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_write_trace_replaces_an_existing_trace(tmp_path):
    write_trace(str(tmp_path), _trace(tmp_path))
    path = write_trace(str(tmp_path), _trace(tmp_path, status="FAIL", stop="cv overflow"))
    assert json.load(open(path))["status"] == "FAIL"
    assert json.load(open(path))["stop"] == "cv overflow"
