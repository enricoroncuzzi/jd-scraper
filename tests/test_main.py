import json
from unittest.mock import MagicMock, patch
import openai
import pytest
from src.models import JobOffer, ScoredOffer
from src.config import AppConfig, SearchConfig, ScoringConfig, TelegramConfig, AutoApplyConfig


def _mock_config(db_url="postgresql://test", autoapply=None):
    return AppConfig(
        search=SearchConfig(
            roles=["AI Engineer", "ML Engineer"],
            location="Europe",
            time_range="r86400",
            work_mode=["remote", "hybrid"],
            countries=["Italy", "Spain"],
        ),
        scoring=ScoringConfig(
            threshold=8,
            exclude_keywords=["VP"],
            priority_keywords=["LLM"],
            candidate_profile="test profile",
        ),
        telegram=TelegramConfig(greeting="Hey!"),
        tier=1,
        llm_api_key="test-key",
        telegram_token="test-token",
        telegram_chat_id="123",
        output_path="/output",
        dedup_log_path="/data/seen.txt",
        db_url=db_url,
        autoapply=autoapply or AutoApplyConfig(),
    )


def _config_with(tmp_path, monkeypatch, tier=1, remote_check=None):
    """Write a real config JSON file on disk and stub the env vars load_config
    needs, so a test can exercise main.handler's actual load_config() call
    instead of monkeypatching main.load_config directly."""
    env = {
        "LLM_API_KEY": "test-llm-key",
        "TELEGRAM_TOKEN": "test-telegram-token",
        "TELEGRAM_CHAT_ID": "test-chat-id",
        "OUTPUT_PATH": str(tmp_path / "output"),
        "DEDUP_LOG_PATH": str(tmp_path / "seen.txt"),
        "GROQ_API_KEY": "test-groq-key",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    data = {
        "tier": tier,
        "search": {
            "roles": ["AI Engineer"],
            "location": "Europe",
            "time_range": "r86400",
            "work_mode": ["remote"],
            "countries": ["Italy"],
        },
        "scoring": {
            "threshold": 8,
            "exclude_keywords": [],
            "priority_keywords": [],
            "candidate_profile": "test profile",
        },
        "telegram": {"greeting": "Hey!"},
    }
    if remote_check is not None:
        data["remote_check"] = {"enabled": remote_check}

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(data))
    return config_path


def test_handler_orchestrates_full_pipeline(monkeypatch):
    raw_offers = [JobOffer(id=0, title="AI Eng", company="Acme", link="https://li.com/0")]
    language_filtered = raw_offers
    new_offers = raw_offers
    scored_offers = [ScoredOffer(id=0, title="AI Eng", company="Acme",
                                  link="https://li.com/0", score=9,
                                  comment="great", summary="LLM role")]

    config = _mock_config()

    mock_fetch = MagicMock(return_value=raw_offers)
    mock_lang_filter = MagicMock(return_value=language_filtered)
    mock_filter = MagicMock(return_value=new_offers)
    mock_score = MagicMock(return_value=(scored_offers, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}))
    mock_init_db = MagicMock()
    mock_save_run = MagicMock(return_value=42)
    mock_save_offers = MagicMock()
    mock_write_notes = MagicMock()
    mock_write_digest = MagicMock()
    mock_write_rejected = MagicMock()
    mock_send = MagicMock()
    mock_mark = MagicMock()
    mock_load_config = MagicMock(return_value=config)

    monkeypatch.setattr("main.load_config", mock_load_config)
    monkeypatch.setattr("main.fetch_offers", mock_fetch)
    monkeypatch.setattr("main.filter_by_language", mock_lang_filter)
    monkeypatch.setattr("main.filter_new", mock_filter)
    monkeypatch.setattr("main.score_offers", mock_score)
    monkeypatch.setattr("main.init_db", mock_init_db)
    monkeypatch.setattr("main.save_run", mock_save_run)
    monkeypatch.setattr("main.save_offers", mock_save_offers)
    monkeypatch.setattr("main.write_notes", mock_write_notes)
    monkeypatch.setattr("main.write_digest", mock_write_digest)
    monkeypatch.setattr("main.write_rejected", mock_write_rejected)
    monkeypatch.setattr("main.send_summary", mock_send)
    monkeypatch.setattr("main.mark_seen", mock_mark)

    import main
    main.handler({}, None)

    mock_load_config.assert_called_once_with("config/config.json")
    mock_fetch.assert_called_once_with(
        roles=["AI Engineer", "ML Engineer"],
        location="Europe",
        time_range="r86400",
        work_modes=["remote", "hybrid"],
        countries=["Italy", "Spain"],
        allowed_countries=None,
    )
    mock_lang_filter.assert_called_once_with(raw_offers)
    mock_filter.assert_called_once_with(language_filtered, "/data/seen.txt")
    mock_score.assert_called_once()
    mock_init_db.assert_called_once_with("postgresql://test")
    mock_save_run.assert_called_once_with(
        "postgresql://test",
        tier=1, offers_fetched=1, offers_new=1,
        prompt_tokens=0, completion_tokens=0, total_tokens=0,
    )
    mock_save_offers.assert_called_once_with("postgresql://test", scored_offers, 42, 1)
    mock_write_notes.assert_called_once_with(scored_offers, "/output", 8, 1)
    mock_write_rejected.assert_called_once()
    mock_write_digest.assert_called_once_with(scored_offers, "/output", 8, tier=1,
                                               verification_enabled=config.remote_check.enabled)
    mock_send.assert_called_once()
    mock_mark.assert_called_once_with(new_offers, "/data/seen.txt")


def test_handler_skips_storage_when_db_url_is_none(monkeypatch):
    raw_offers = [JobOffer(id=0, title="AI Eng", company="Acme", link="https://li.com/0")]
    scored_offers = [ScoredOffer(id=0, title="AI Eng", company="Acme",
                                  link="https://li.com/0", score=9,
                                  comment="great", summary="LLM role")]

    mock_load_config = MagicMock(return_value=_mock_config(db_url=None))
    mock_fetch = MagicMock(return_value=raw_offers)
    mock_lang_filter = MagicMock(return_value=raw_offers)
    mock_filter = MagicMock(return_value=raw_offers)
    mock_score = MagicMock(return_value=(scored_offers, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}))
    mock_init_db = MagicMock()
    mock_save_run = MagicMock()
    mock_write_notes = MagicMock()
    mock_write_digest = MagicMock()
    mock_send = MagicMock()
    mock_mark = MagicMock()

    monkeypatch.setattr("main.load_config", mock_load_config)
    monkeypatch.setattr("main.fetch_offers", mock_fetch)
    monkeypatch.setattr("main.filter_by_language", mock_lang_filter)
    monkeypatch.setattr("main.filter_new", mock_filter)
    monkeypatch.setattr("main.score_offers", mock_score)
    monkeypatch.setattr("main.init_db", mock_init_db)
    monkeypatch.setattr("main.save_run", mock_save_run)
    monkeypatch.setattr("main.write_notes", mock_write_notes)
    monkeypatch.setattr("main.write_digest", mock_write_digest)
    monkeypatch.setattr("main.write_rejected", lambda *a, **kw: None)
    monkeypatch.setattr("main.send_summary", mock_send)
    monkeypatch.setattr("main.mark_seen", mock_mark)

    import main
    main.handler({}, None)

    mock_init_db.assert_not_called()
    mock_save_run.assert_not_called()


def test_handler_skips_pipeline_when_no_new_offers(monkeypatch):
    mock_load_config = MagicMock(return_value=_mock_config())
    mock_fetch = MagicMock(return_value=[JobOffer(id=0, title="t", company="c", link="l")])
    mock_lang_filter = MagicMock(side_effect=lambda offers: offers)
    mock_filter = MagicMock(return_value=[])
    mock_score = MagicMock()
    mock_send_message = MagicMock()
    mock_send_summary = MagicMock()

    monkeypatch.setattr("main.load_config", mock_load_config)
    monkeypatch.setattr("main.fetch_offers", mock_fetch)
    monkeypatch.setattr("main.filter_by_language", mock_lang_filter)
    monkeypatch.setattr("main.filter_new", mock_filter)
    monkeypatch.setattr("main.score_offers", mock_score)
    monkeypatch.setattr("main.send_message", mock_send_message)
    monkeypatch.setattr("main.send_summary", mock_send_summary)

    import main
    main.handler({}, None)

    mock_score.assert_not_called()
    # Genuinely-empty case (nothing found at all): stays a silent early return,
    # distinct from the all-rejected-by-verification case which now notifies.
    mock_send_message.assert_not_called()
    mock_send_summary.assert_not_called()


def test_handler_logs_description_quality_summary(monkeypatch, tmp_path, capsys):
    # patch fetch_offers to return two offers with known statuses
    offers = [
        JobOffer(id=0, title="AI Eng", company="A", link="https://li.com/0",
                 description="text", description_status="ok"),
        JobOffer(id=1, title="ML Eng", company="B", link="https://li.com/1",
                 description="", description_status="failed"),
    ]
    config = _mock_config()
    monkeypatch.setattr("main.load_config", lambda *a, **kw: config)
    monkeypatch.setattr("main.fetch_offers", lambda **kw: offers)
    monkeypatch.setattr("main.filter_by_language", lambda x: x)
    monkeypatch.setattr("main.filter_new", lambda x, p: x)
    monkeypatch.setattr("main.score_offers", lambda **kw: (
        [ScoredOffer(**o.model_dump(), score=7, comment="c", summary="s") for o in offers],
        {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    ))
    monkeypatch.setattr("main.write_notes", lambda *a, **kw: None)
    monkeypatch.setattr("main.write_digest", lambda *a, **kw: None)
    monkeypatch.setattr("main.write_rejected", lambda *a, **kw: None)
    monkeypatch.setattr("main.send_summary", lambda **kw: None)
    monkeypatch.setattr("main.mark_seen", lambda *a: None)
    monkeypatch.setattr("main.init_db", lambda *a: None)
    monkeypatch.setattr("main.save_run", lambda *a, **kw: 0)
    monkeypatch.setattr("main.save_offers", lambda *a, **kw: None)
    monkeypatch.setattr("main.os.makedirs", lambda *a, **kw: None)
    monkeypatch.setattr("builtins.open", lambda *a, **kw: __import__("io").StringIO())

    import main
    main.handler({}, None, config_path="config/config_tier1.json")

    out = capsys.readouterr().out
    assert "Description quality" in out
    assert "ok: 1" in out
    assert "failed: 1" in out


def test_handler_skips_autoapply_when_disabled(monkeypatch):
    config = _mock_config(autoapply=AutoApplyConfig(enabled=False))
    scored_offers = [ScoredOffer(id=0, title="AI Eng", company="Acme",
                                  link="https://li.com/0", score=9,
                                  comment="great", summary="LLM role")]

    monkeypatch.setattr("main.load_config", lambda *a, **kw: config)
    monkeypatch.setattr("main.fetch_offers", lambda **kw: [scored_offers[0]])
    monkeypatch.setattr("main.filter_by_language", lambda x: x)
    monkeypatch.setattr("main.filter_new", lambda x, p: x)
    monkeypatch.setattr("main.score_offers", lambda **kw: (
        scored_offers, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    ))
    monkeypatch.setattr("main.write_notes", lambda *a, **kw: None)
    monkeypatch.setattr("main.write_digest", lambda *a, **kw: None)
    monkeypatch.setattr("main.write_rejected", lambda *a, **kw: None)
    monkeypatch.setattr("main.send_summary", lambda **kw: None)
    monkeypatch.setattr("main.mark_seen", lambda *a: None)
    monkeypatch.setattr("main.init_db", lambda *a: None)
    monkeypatch.setattr("main.save_run", lambda *a, **kw: 0)
    monkeypatch.setattr("main.save_offers", lambda *a, **kw: None)
    mock_autoapply = MagicMock()
    monkeypatch.setattr("main.run_autoapply", mock_autoapply)

    import main
    main.handler({}, None)

    mock_autoapply.assert_not_called()


def test_handler_runs_autoapply_when_enabled(monkeypatch):
    config = _mock_config(autoapply=AutoApplyConfig(enabled=True, dry_run=True, daily_cap=3))
    scored_offers = [ScoredOffer(id=0, title="AI Eng", company="Acme",
                                  link="https://li.com/0", score=9,
                                  comment="great", summary="LLM role")]

    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.setattr("main.load_config", lambda *a, **kw: config)
    monkeypatch.setattr("main.fetch_offers", lambda **kw: [scored_offers[0]])
    monkeypatch.setattr("main.filter_by_language", lambda x: x)
    monkeypatch.setattr("main.filter_new", lambda x, p: x)
    monkeypatch.setattr("main.score_offers", lambda **kw: (
        scored_offers, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    ))
    monkeypatch.setattr("main.write_notes", lambda *a, **kw: None)
    monkeypatch.setattr("main.write_digest", lambda *a, **kw: None)
    monkeypatch.setattr("main.write_rejected", lambda *a, **kw: None)
    monkeypatch.setattr("main.send_summary", lambda **kw: None)
    monkeypatch.setattr("main.mark_seen", lambda *a: None)
    monkeypatch.setattr("main.init_db", lambda *a: None)
    monkeypatch.setattr("main.save_run", lambda *a, **kw: 0)
    monkeypatch.setattr("main.save_offers", lambda *a, **kw: None)
    mock_autoapply = MagicMock(return_value=[])
    monkeypatch.setattr("main.run_autoapply", mock_autoapply)

    import main
    main.handler({}, None)

    mock_autoapply.assert_called_once()
    kwargs = mock_autoapply.call_args.kwargs
    assert kwargs["offers"] == scored_offers
    assert kwargs["threshold"] == 8
    assert kwargs["tier"] == 1
    assert kwargs["daily_cap"] == 3
    assert kwargs["dry_run"] is True
    assert kwargs["groq_api_key"] == "test-groq-key"
    assert kwargs["telegram_token"] == config.telegram_token
    assert kwargs["telegram_chat_id"] == config.telegram_chat_id


def test_handler_survives_autoapply_failure_and_still_sends_digest(monkeypatch):
    config = _mock_config(autoapply=AutoApplyConfig(enabled=True, dry_run=False, daily_cap=3))
    scored_offers = [ScoredOffer(id=0, title="AI Eng", company="Acme",
                                  link="https://li.com/0", score=9,
                                  comment="great", summary="LLM role")]

    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.setattr("main.load_config", lambda *a, **kw: config)
    monkeypatch.setattr("main.fetch_offers", lambda **kw: [scored_offers[0]])
    monkeypatch.setattr("main.filter_by_language", lambda x: x)
    monkeypatch.setattr("main.filter_new", lambda x, p: x)
    monkeypatch.setattr("main.score_offers", lambda **kw: (
        scored_offers, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    ))
    monkeypatch.setattr("main.write_notes", lambda *a, **kw: None)
    monkeypatch.setattr("main.write_digest", lambda *a, **kw: None)
    monkeypatch.setattr("main.write_rejected", lambda *a, **kw: None)
    mock_send_summary = MagicMock()
    monkeypatch.setattr("main.send_summary", mock_send_summary)
    monkeypatch.setattr("main.mark_seen", lambda *a: None)
    monkeypatch.setattr("main.init_db", lambda *a: None)
    monkeypatch.setattr("main.save_run", lambda *a, **kw: 0)
    monkeypatch.setattr("main.save_offers", lambda *a, **kw: None)
    monkeypatch.setattr(
        "main.run_autoapply",
        MagicMock(side_effect=FileNotFoundError("CV master source not found at '/bad/path'")),
    )
    mock_send_message = MagicMock()
    monkeypatch.setattr("main.send_message", mock_send_message)

    import main
    main.handler({}, None)  # must not raise

    mock_send_message.assert_called_once()
    text = mock_send_message.call_args.args[0]
    assert "auto-apply FAILED" in text
    assert "CV master source not found" in text
    mock_send_summary.assert_called_once()  # the regular digest still goes out


def test_handler_survives_autoapply_failure_notification_also_failing(monkeypatch, capsys):
    config = _mock_config(autoapply=AutoApplyConfig(enabled=True, dry_run=False, daily_cap=3))
    scored_offers = [ScoredOffer(id=0, title="AI Eng", company="Acme",
                                  link="https://li.com/0", score=9,
                                  comment="great", summary="LLM role")]

    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.setattr("main.load_config", lambda *a, **kw: config)
    monkeypatch.setattr("main.fetch_offers", lambda **kw: [scored_offers[0]])
    monkeypatch.setattr("main.filter_by_language", lambda x: x)
    monkeypatch.setattr("main.filter_new", lambda x, p: x)
    monkeypatch.setattr("main.score_offers", lambda **kw: (
        scored_offers, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    ))
    monkeypatch.setattr("main.write_notes", lambda *a, **kw: None)
    monkeypatch.setattr("main.write_digest", lambda *a, **kw: None)
    monkeypatch.setattr("main.write_rejected", lambda *a, **kw: None)
    monkeypatch.setattr("main.send_summary", lambda **kw: None)
    monkeypatch.setattr("main.mark_seen", lambda *a: None)
    monkeypatch.setattr("main.init_db", lambda *a: None)
    monkeypatch.setattr("main.save_run", lambda *a, **kw: 0)
    monkeypatch.setattr("main.save_offers", lambda *a, **kw: None)
    monkeypatch.setattr("main.run_autoapply", MagicMock(side_effect=RuntimeError("telegram POST failed")))
    monkeypatch.setattr("main.send_message", MagicMock(side_effect=RuntimeError("network down")))

    import main
    main.handler({}, None)  # must not raise even when the failure notice itself can't send

    assert "Failed to send auto-apply failure notification" in capsys.readouterr().out


def test_run_tier_with_retry_retries_transient_failure_then_succeeds(monkeypatch):
    call_count = {"n": 0}

    def flaky_handler(event, context, config_path="config/config.json"):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise RuntimeError("LinkedIn search returned 503 after 20 retries")

    sleeps = []
    monkeypatch.setattr("main.handler", flaky_handler)
    mock_notify = MagicMock()
    monkeypatch.setattr("main._notify_failure", mock_notify)

    import main
    main.run_tier_with_retry("config/config_tier1.json", sleep=sleeps.append)

    assert call_count["n"] == 3
    assert sleeps == [60, 120]  # increasing delay, default base 60s
    mock_notify.assert_not_called()


def test_run_tier_with_retry_gives_up_and_notifies_after_max_attempts(monkeypatch):
    def always_fails(event, context, config_path="config/config.json"):
        raise ValueError("504 gateway timeout")

    monkeypatch.setattr("main.handler", always_fails)
    mock_notify = MagicMock()
    monkeypatch.setattr("main._notify_failure", mock_notify)

    import main
    with pytest.raises(ValueError):
        main.run_tier_with_retry("config/config_tier1.json", sleep=lambda s: None)

    mock_notify.assert_called_once()
    args = mock_notify.call_args.args
    assert args[0] == "config/config_tier1.json"
    assert isinstance(args[1], ValueError)
    assert args[3] is True  # retryable: exhausted attempts, not quota


def test_run_tier_with_retry_does_not_retry_quota_exhaustion(monkeypatch):
    call_count = {"n": 0}
    reset_ms = int((__import__("time").time() + 3600) * 1000)

    def quota_fails(event, context, config_path="config/config.json"):
        call_count["n"] += 1
        raise openai.RateLimitError(
            "rate limited",
            response=MagicMock(status_code=429, headers={"x-ratelimit-reset": str(reset_ms)}),
            body={"code": 429, "metadata": {"headers": {"X-RateLimit-Reset": str(reset_ms)}}},
        )

    monkeypatch.setattr("main.handler", quota_fails)
    mock_notify = MagicMock()
    monkeypatch.setattr("main._notify_failure", mock_notify)

    def fail_if_called(_):
        raise AssertionError("should not sleep/retry on quota exhaustion")

    import main
    with pytest.raises(openai.RateLimitError):
        main.run_tier_with_retry("config/config_tier1.json", sleep=fail_if_called)

    assert call_count["n"] == 1  # no retry loop for quota exhaustion
    mock_notify.assert_called_once()
    args = mock_notify.call_args.args
    assert args[3] is False  # retryable=False (quota)


def test_notify_failure_sends_telegram_message(monkeypatch):
    config = _mock_config()
    monkeypatch.setattr("main.load_config", lambda *a, **kw: config)
    mock_send = MagicMock()
    monkeypatch.setattr("main.send_message", mock_send)

    import main
    main._notify_failure("config/config_tier1.json", RuntimeError("boom"), attempts=4, retryable=True)

    mock_send.assert_called_once()
    text, token, chat_id = mock_send.call_args.args
    assert "Tier 1" in text
    assert "FAILED" in text
    assert "boom" in text
    assert token == config.telegram_token
    assert chat_id == config.telegram_chat_id


def test_notify_failure_survives_telegram_send_error(monkeypatch, capsys):
    config = _mock_config()
    monkeypatch.setattr("main.load_config", lambda *a, **kw: config)
    monkeypatch.setattr("main.send_message", MagicMock(side_effect=RuntimeError("network down")))

    import main
    main._notify_failure("config/config_tier1.json", RuntimeError("boom"), attempts=1, retryable=False)

    assert "Failed to send failure notification" in capsys.readouterr().out


def _stub_common_pipeline(monkeypatch):
    monkeypatch.setattr("main.write_notes", lambda *a, **kw: None)
    monkeypatch.setattr("main.write_digest", lambda *a, **kw: None)
    monkeypatch.setattr("main.write_rejected", lambda *a, **kw: None)
    monkeypatch.setattr("main.send_summary", lambda **kw: None)
    monkeypatch.setattr("main.init_db", lambda *a: None)
    monkeypatch.setattr("main.save_run", lambda *a, **kw: 0)
    monkeypatch.setattr("main.save_offers", lambda *a, **kw: None)
    monkeypatch.setattr("main.run_autoapply", lambda **kw: [])


def test_rejected_offers_are_dropped_before_scoring_but_still_marked_seen(monkeypatch, tmp_path):
    from src.models import JobOffer
    fetched = [
        JobOffer(id=1, title="Good", company="A", link="https://x/1", description="d"),
        JobOffer(id=2, title="Bad", company="B", link="https://x/2", description="d"),
    ]

    def fake_verify(offers, require_italy_eligibility, groq_api_key):
        offers[0].remote_verdict = "confirmed"
        offers[1].remote_verdict = "rejected"
        return offers, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    scored_input = {}

    def fake_score(offers, **kwargs):
        scored_input["ids"] = [o.id for o in offers]
        return [], {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    marked = {}
    import main
    monkeypatch.setattr("main.fetch_offers", lambda **kwargs: fetched)
    monkeypatch.setattr("main.filter_by_language", lambda offers: offers)
    monkeypatch.setattr("main.filter_new", lambda offers, path: offers)
    monkeypatch.setattr("main.verify_offers", fake_verify)
    monkeypatch.setattr("main.score_offers", fake_score)
    monkeypatch.setattr("main.mark_seen", lambda offers, path: marked.update(ids=[o.id for o in offers]))
    _stub_common_pipeline(monkeypatch)

    main.handler({}, None, config_path=str(_config_with(tmp_path, monkeypatch, remote_check=True)))

    assert scored_input["ids"] == [1]
    assert sorted(marked["ids"]) == [1, 2]


def test_all_offers_rejected_by_verification_notifies_and_still_marks_seen(monkeypatch, tmp_path):
    from src.models import JobOffer
    fetched = [
        JobOffer(id=1, title="Bad1", company="A", link="https://x/1", description="d"),
        JobOffer(id=2, title="Bad2", company="B", link="https://x/2", description="d"),
    ]

    def fake_verify(offers, require_italy_eligibility, groq_api_key):
        for o in offers:
            o.remote_verdict = "rejected"
        return offers, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    mock_score = MagicMock()
    marked = {}
    mock_send_message = MagicMock()
    import main
    monkeypatch.setattr("main.fetch_offers", lambda **kwargs: fetched)
    monkeypatch.setattr("main.filter_by_language", lambda offers: offers)
    monkeypatch.setattr("main.filter_new", lambda offers, path: offers)
    monkeypatch.setattr("main.verify_offers", fake_verify)
    monkeypatch.setattr("main.score_offers", mock_score)
    monkeypatch.setattr("main.mark_seen", lambda offers, path: marked.update(ids=[o.id for o in offers]))
    monkeypatch.setattr("main.send_message", mock_send_message)
    _stub_common_pipeline(monkeypatch)

    main.handler({}, None, config_path=str(_config_with(tmp_path, monkeypatch, remote_check=True)))

    mock_score.assert_not_called()
    mock_send_message.assert_called_once()
    text = mock_send_message.call_args.args[0]
    assert "2" in text
    assert "rejected" in text
    assert sorted(marked["ids"]) == [1, 2]


def test_verification_is_skipped_when_disabled(monkeypatch, tmp_path):
    import main
    called = {"verify": False}
    monkeypatch.setattr("main.fetch_offers", lambda **kw: [
        JobOffer(id=1, title="Good", company="A", link="https://x/1", description="d"),
    ])
    monkeypatch.setattr("main.filter_by_language", lambda offers: offers)
    monkeypatch.setattr("main.filter_new", lambda offers, path: offers)
    monkeypatch.setattr("main.verify_offers",
                        lambda *a, **k: called.update(verify=True) or ([], {}))
    monkeypatch.setattr("main.score_offers", lambda **kw: (
        [], {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    ))
    monkeypatch.setattr("main.mark_seen", lambda *a: None)
    _stub_common_pipeline(monkeypatch)

    main.handler({}, None, config_path=str(_config_with(tmp_path, monkeypatch, remote_check=False)))
    assert called["verify"] is False


def test_tier3_passes_the_scope_filter_to_the_scraper(monkeypatch, tmp_path):
    from src.tier_scope import TIER3_ALLOWED_COUNTRIES
    import main
    seen = {}
    monkeypatch.setattr("main.fetch_offers", lambda **kwargs: seen.update(kwargs) or [])
    main.handler({}, None, config_path=str(_config_with(tmp_path, monkeypatch, tier=3)))
    assert seen["allowed_countries"] == TIER3_ALLOWED_COUNTRIES


def _all_rejected_run(monkeypatch, tmp_path, mock_save_run, db_url):
    from src.models import JobOffer
    fetched = [
        JobOffer(id=1, title="Bad1", company="A", link="https://x/1", description="d"),
        JobOffer(id=2, title="Bad2", company="B", link="https://x/2", description="d"),
    ]

    def fake_verify(offers, require_italy_eligibility, groq_api_key):
        for o in offers:
            o.remote_verdict = "rejected"
        return offers, {"prompt_tokens": 70, "completion_tokens": 30, "total_tokens": 100}

    import main
    monkeypatch.setattr("main.fetch_offers", lambda **kwargs: fetched)
    monkeypatch.setattr("main.filter_by_language", lambda offers: offers)
    monkeypatch.setattr("main.filter_new", lambda offers, path: offers)
    monkeypatch.setattr("main.verify_offers", fake_verify)
    monkeypatch.setattr("main.score_offers", MagicMock())
    monkeypatch.setattr("main.mark_seen", lambda offers, path: None)
    monkeypatch.setattr("main.send_message", MagicMock())
    _stub_common_pipeline(monkeypatch)
    monkeypatch.setattr("main.save_run", mock_save_run)
    if db_url:
        monkeypatch.setenv("DATABASE_URL", db_url)
    else:
        monkeypatch.delenv("DATABASE_URL", raising=False)

    main.handler({}, None, config_path=str(_config_with(tmp_path, monkeypatch, remote_check=True)))


def test_all_rejected_run_is_still_recorded_in_storage(monkeypatch, tmp_path):
    mock_save_run = MagicMock(return_value=7)

    _all_rejected_run(monkeypatch, tmp_path, mock_save_run, db_url="postgresql://test")

    mock_save_run.assert_called_once()
    kwargs = mock_save_run.call_args.kwargs
    assert kwargs["tier"] == 1
    assert kwargs["offers_fetched"] == 2
    assert kwargs["offers_new"] == 2
    assert kwargs["total_tokens"] == 100
    assert kwargs["prompt_tokens"] == 70
    assert kwargs["completion_tokens"] == 30


def test_all_rejected_run_skips_storage_when_db_url_is_none(monkeypatch, tmp_path):
    mock_save_run = MagicMock()

    _all_rejected_run(monkeypatch, tmp_path, mock_save_run, db_url=None)

    mock_save_run.assert_not_called()


def test_all_rejected_run_survives_a_storage_failure(monkeypatch, tmp_path, capsys):
    mock_save_run = MagicMock(side_effect=RuntimeError("neon down"))

    _all_rejected_run(monkeypatch, tmp_path, mock_save_run, db_url="postgresql://test")

    assert "[storage] Failed" in capsys.readouterr().out
