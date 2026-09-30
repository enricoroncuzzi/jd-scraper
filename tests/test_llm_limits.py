import json
from src.llm_limits import Limit, load_limits, limit_for


def _write(tmp_path, payload):
    path = tmp_path / "limits.json"
    path.write_text(json.dumps(payload) if not isinstance(payload, str) else payload)
    return str(path)


def test_load_limits_reads_every_entry(tmp_path):
    path = _write(tmp_path, {"limits": [
        {"provider": "groq", "model": "openai/gpt-oss-20b", "unit": "tokens", "per_day": 200000},
        {"provider": "openrouter", "model": "*", "unit": "requests", "per_day": None},
    ]})
    assert load_limits(path) == [
        Limit("groq", "openai/gpt-oss-20b", "tokens", 200000),
        Limit("openrouter", "*", "requests", None),
    ]


def test_missing_or_malformed_file_yields_no_limits(tmp_path):
    assert load_limits(str(tmp_path / "absent.json")) == []
    assert load_limits(_write(tmp_path, "{not json")) == []
    assert load_limits(_write(tmp_path, {"limits": "nope"})) == []


def test_bad_entries_are_skipped_not_fatal(tmp_path):
    path = _write(tmp_path, {"limits": [
        {"provider": "groq", "model": "m", "unit": "furlongs", "per_day": 5},
        {"provider": "groq", "model": "m2", "unit": "tokens", "per_day": -1},
        {"provider": "groq", "model": "m3", "unit": "tokens", "per_day": "lots"},
        {"provider": "groq", "model": "ok", "unit": "tokens", "per_day": 10},
    ]})
    assert load_limits(path) == [Limit("groq", "ok", "tokens", 10)]


def test_limit_for_prefers_exact_model_over_wildcard():
    limits = [Limit("openrouter", "*", "requests", 1000), Limit("openrouter", "x/y", "tokens", 5)]
    assert limit_for("openrouter", "x/y", limits).unit == "tokens"
    assert limit_for("openrouter", "other", limits).unit == "requests"
    assert limit_for("groq", "x/y", limits) is None


def test_shipped_config_parses_and_keeps_the_verifier_budget():
    limit = limit_for("groq", "openai/gpt-oss-20b", load_limits())
    assert limit == Limit("groq", "openai/gpt-oss-20b", "tokens", 200000)


def test_default_path_loads_from_any_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_limits() != []
