import json
import pytest
from src.config import load_config


def test_load_config_reads_json_and_env(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({
        "search": {
            "roles": ["AI Engineer", "ML Engineer"],
            "location": "Europe",
            "time_range": "r86400",
            "work_mode": ["remote", "hybrid"]
        },
        "scoring": {
            "threshold": 8,
            "exclude_keywords": ["VP"],
            "priority_keywords": ["LLM"],
            "candidate_profile": "test profile"
        },
        "telegram": {"greeting": "Hey!"}
    }))
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.setenv("TELEGRAM_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456")
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", "/vault")
    monkeypatch.setenv("DEDUP_LOG_PATH", "/data/seen.txt")

    config = load_config(str(config_file))

    assert config.search.roles == ["AI Engineer", "ML Engineer"]
    assert config.search.location == "Europe"
    assert config.search.time_range == "r86400"
    assert config.search.work_mode == ["remote", "hybrid"]
    assert config.scoring.threshold == 8
    assert config.scoring.exclude_keywords == ["VP"]
    assert config.scoring.priority_keywords == ["LLM"]
    assert config.scoring.candidate_profile == "test profile"
    assert config.telegram.greeting == "Hey!"
    assert config.groq_api_key == "test-groq-key"
    assert config.telegram_token == "test-token"
    assert config.telegram_chat_id == "123456"
    assert config.obsidian_vault_path == "/vault"
    assert config.dedup_log_path == "/data/seen.txt"


def test_load_config_raises_on_missing_env(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({
        "search": {
            "roles": ["AI Engineer"],
            "location": "Europe",
            "time_range": "r86400",
            "work_mode": ["remote"]
        },
        "scoring": {"threshold": 8, "exclude_keywords": [], "priority_keywords": [], "candidate_profile": "x"},
        "telegram": {"greeting": "Hi"}
    }))
    monkeypatch.setattr("src.config.load_dotenv", lambda: None)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    with pytest.raises(KeyError):
        load_config(str(config_file))
