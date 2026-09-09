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


def _config_with(tmp_path, monkeypatch, tier=1, remote_check=None, allowed_countries=None):
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

    search = {
        "roles": ["AI Engineer"],
        "location": "Europe",
        "time_range": "r86400",
        "work_mode": ["remote"],
        "countries": ["Italy"],
    }
    if allowed_countries is not None:
        search["allowed_countries"] = allowed_countries

    data = {
        "tier": tier,
        "search": search,
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
                                               verification_enabled=config.remote_check.enabled,
                                               deferred_count=0)
    mock_send.assert_called_once()
    # "Seen" means handled (rejected by verification, or scored), not fetched:
    # everything scoring never reached stays new to the next run.
    mock_mark.assert_called_once()
    marked, log_path = mock_mark.call_args.args
    assert [o.link for o in marked] == ["https://li.com/0"]
    assert log_path == "/data/seen.txt"


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
        scored_input["links"] = [o.link for o in offers]
        return ([ScoredOffer(**o.model_dump(), score=9, comment="c", summary="s") for o in offers],
                {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})

    marked = {}
    import main
    monkeypatch.setattr("main.fetch_offers", lambda **kwargs: fetched)
    monkeypatch.setattr("main.filter_by_language", lambda offers: offers)
    monkeypatch.setattr("main.filter_new", lambda offers, path: offers)
    monkeypatch.setattr("main.verify_offers", fake_verify)
    monkeypatch.setattr("main.score_offers", fake_score)
    monkeypatch.setattr("main.mark_seen",
                        lambda offers, path: marked.update(links=[o.link for o in offers]))
    _stub_common_pipeline(monkeypatch)

    main.handler({}, None, config_path=str(_config_with(tmp_path, monkeypatch, remote_check=True)))

    assert scored_input["links"] == ["https://x/1"]
    assert sorted(marked["links"]) == ["https://x/1", "https://x/2"]


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
    main.handler({}, None, config_path=str(_config_with(
        tmp_path, monkeypatch, tier=3, allowed_countries=sorted(TIER3_ALLOWED_COUNTRIES),
    )))
    assert seen["allowed_countries"] == TIER3_ALLOWED_COUNTRIES


def test_tier2_passes_its_scope_to_the_scraper(monkeypatch, tmp_path):
    import main
    seen = {}
    monkeypatch.setattr("main.fetch_offers", lambda **kwargs: seen.update(kwargs) or [])
    main.handler({}, None, config_path=str(_config_with(
        tmp_path, monkeypatch, tier=2, allowed_countries=["Switzerland", "San Marino"],
    )))
    assert seen["allowed_countries"] == frozenset({"switzerland", "san marino"})


def test_tiers_without_a_scope_do_no_narrowing(monkeypatch, tmp_path):
    import main
    for tier in (1, 4):
        seen = {}
        monkeypatch.setattr("main.fetch_offers", lambda **kwargs: seen.update(kwargs) or [])
        main.handler({}, None, config_path=str(_config_with(tmp_path, monkeypatch, tier=tier)))
        assert seen["allowed_countries"] is None


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


def test_groq_verification_tokens_today_sums_only_todays_verification_entries(monkeypatch, tmp_path):
    import main
    from datetime import datetime, timedelta

    usage_log = tmp_path / "usage_log.jsonl"
    monkeypatch.setattr("main._USAGE_LOG_PATH", str(usage_log))

    today = datetime.now().isoformat(timespec="seconds")
    yesterday = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
    usage_log.write_text("\n".join(json.dumps(entry) for entry in [
        {"timestamp": today, "stage": "verification", "total_tokens": 1000},
        {"timestamp": today, "stage": "verification", "total_tokens": 2500},
        {"timestamp": today, "stage": "scoring", "total_tokens": 9999},  # different stage, excluded
        {"timestamp": yesterday, "stage": "verification", "total_tokens": 7777},  # different day, excluded
    ]) + "\n")

    assert main._groq_verification_tokens_today() == 3500


def test_groq_verification_tokens_today_is_zero_when_log_missing(monkeypatch, tmp_path):
    import main
    monkeypatch.setattr("main._USAGE_LOG_PATH", str(tmp_path / "does_not_exist.jsonl"))
    assert main._groq_verification_tokens_today() == 0


def test_handler_logs_verification_token_usage_against_daily_limit(monkeypatch, tmp_path, capsys):
    import main
    fetched = [JobOffer(id=1, title="Good", company="A", link="https://x/1", description="d")]

    def fake_verify(offers, require_italy_eligibility, groq_api_key):
        offers[0].remote_verdict = "confirmed"
        return offers, {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50}

    monkeypatch.setattr("main.fetch_offers", lambda **kwargs: fetched)
    monkeypatch.setattr("main.filter_by_language", lambda offers: offers)
    monkeypatch.setattr("main.filter_new", lambda offers, path: offers)
    monkeypatch.setattr("main.verify_offers", fake_verify)
    monkeypatch.setattr("main.score_offers", lambda **kwargs: (
        [], {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    ))
    monkeypatch.setattr("main.mark_seen", lambda *a: None)
    monkeypatch.setattr("main._USAGE_LOG_PATH", str(tmp_path / "usage_log.jsonl"))
    _stub_common_pipeline(monkeypatch)

    main.handler({}, None, config_path=str(_config_with(tmp_path, monkeypatch, remote_check=True)))

    out = capsys.readouterr().out
    assert "Verification token usage" in out
    assert "total: 50" in out
    assert f"Groq verification total today: 50/{main._GROQ_DAILY_TOKEN_LIMIT}" in out


# --- the deferred-offer retry queue (scoring dies mid-tier) -----------------

_ZERO_USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _offer(i, link=None):
    return JobOffer(id=i, title=f"Role {i}", company="A",
                    link=link or f"https://x/{i}", description="a real description")


def _score_all(offers, **kwargs):
    return ([ScoredOffer(**o.model_dump(), score=9, comment="c", summary="s") for o in offers],
            dict(_ZERO_USAGE))


def _score_nothing(offers, **kwargs):
    return [], dict(_ZERO_USAGE)


def _score_first_n(n):
    """Stands in for src/scorer.py's partial-save behaviour: score_offers
    returns what it scored and stops when a batch dies after all retries."""
    def fake(offers, **kwargs):
        return ([ScoredOffer(**o.model_dump(), score=9, comment="c", summary="s")
                 for o in offers[:n]], dict(_ZERO_USAGE))
    return fake


def _queue_path(tmp_path, tier=1):
    return str(tmp_path / f"unscored_tier{tier}.jsonl")


def _seen_path(tmp_path):
    return str(tmp_path / "seen.txt")


def _seed_queue(tmp_path, offers, tier=1, age=None):
    from src.retry_queue import QueueEntry, build_deferred, save_deferred
    from datetime import datetime, timedelta
    if age is None:
        entries = build_deferred(list(offers), [])
    else:
        entries = [QueueEntry(queued_at=datetime.now() - age, offer=o) for o in offers]
    save_deferred(_queue_path(tmp_path, tier), entries)


def _run_handler(monkeypatch, tmp_path, fetched, score=_score_all, tier=1,
                 remote_check=None, verify=None):
    """Run main.handler against the real dedup log and retry queue in tmp_path,
    with only the outward-facing stages stubbed. Returns the offers scoring was
    handed and the send_summary mock."""
    config_path = _config_with(tmp_path, monkeypatch, tier=tier, remote_check=remote_check)
    calls = {"score_input": []}

    def fake_score(offers, **kwargs):
        calls["score_input"] = list(offers)
        return score(offers, **kwargs)

    import main
    _stub_common_pipeline(monkeypatch)
    monkeypatch.setattr("main.fetch_offers", lambda **kwargs: list(fetched))
    monkeypatch.setattr("main.filter_by_language", lambda offers: offers)
    monkeypatch.setattr("main.score_offers", fake_score)
    monkeypatch.setattr("main._USAGE_LOG_PATH", str(tmp_path / "usage_log.jsonl"))
    if verify is not None:
        monkeypatch.setattr("main.verify_offers", verify)
    mock_send_summary = MagicMock()
    monkeypatch.setattr("main.send_summary", mock_send_summary)

    main.handler({}, None, config_path=str(config_path))
    return calls, mock_send_summary


def test_offers_scoring_never_reached_are_queued_and_not_marked_seen(monkeypatch, tmp_path):
    """The 2026-09-07 tier-4 loss in miniature: 5 new offers, scoring dies
    after 2. The unscored 3 must stay out of the dedup log, land in the retry
    queue with their descriptions, and be reported in the summary."""
    from src.dedup import filter_new
    from src.retry_queue import load_deferred
    fetched = [_offer(i) for i in range(5)]

    _, send_summary = _run_handler(monkeypatch, tmp_path, fetched, score=_score_first_n(2))

    assert [o.link for o in filter_new(fetched, _seen_path(tmp_path))] == [
        "https://x/2", "https://x/3", "https://x/4",
    ]
    queue = load_deferred(_queue_path(tmp_path))
    assert [e.offer.link for e in queue] == ["https://x/2", "https://x/3", "https://x/4"]
    assert queue[0].offer.description == "a real description"
    assert send_summary.call_args.kwargs["deferred_count"] == 3


def test_a_run_where_scoring_produces_nothing_defers_every_survivor(monkeypatch, tmp_path):
    from src.retry_queue import load_deferred
    fetched = [_offer(i) for i in range(3)]

    _, send_summary = _run_handler(monkeypatch, tmp_path, fetched, score=_score_nothing)

    assert open(_seen_path(tmp_path)).read().strip() == ""
    assert len(load_deferred(_queue_path(tmp_path))) == 3
    assert send_summary.call_args.kwargs["deferred_count"] == 3


def test_the_next_run_scores_the_queue_ahead_of_fresh_offers(monkeypatch, tmp_path):
    from src.dedup import filter_new
    from src.retry_queue import load_deferred
    queued = [_offer(101), _offer(102)]
    _seed_queue(tmp_path, queued)

    calls, send_summary = _run_handler(monkeypatch, tmp_path, [_offer(1)])

    assert [o.link for o in calls["score_input"]] == [
        "https://x/101", "https://x/102", "https://x/1",
    ]
    assert filter_new(queued + [_offer(1)], _seen_path(tmp_path)) == []
    assert load_deferred(_queue_path(tmp_path)) == []
    assert send_summary.call_args.kwargs["deferred_count"] == 0


def test_a_queued_offer_scraped_again_today_is_scored_once(monkeypatch, tmp_path):
    # A deferred offer is not in the dedup log, so today's scrape can return it
    # again. The fresh copy wins (newer description and verdict) and it must
    # not be scored twice.
    from src.retry_queue import load_deferred
    _seed_queue(tmp_path, [_offer(1)])

    calls, _ = _run_handler(monkeypatch, tmp_path, [_offer(1), _offer(2)])

    assert [o.link for o in calls["score_input"]] == ["https://x/1", "https://x/2"]
    assert load_deferred(_queue_path(tmp_path)) == []


def test_a_run_with_no_new_offers_still_drains_the_queue(monkeypatch, tmp_path):
    from src.dedup import _hash, filter_new
    queued = [_offer(1)]
    _seed_queue(tmp_path, queued)
    open(_seen_path(tmp_path), "w").write(_hash("https://x/9") + "\n")

    calls, send_summary = _run_handler(monkeypatch, tmp_path, [_offer(9)])

    assert [o.link for o in calls["score_input"]] == ["https://x/1"]
    assert filter_new(queued, _seen_path(tmp_path)) == []
    assert send_summary.call_args.kwargs["deferred_count"] == 0


def test_an_offer_deferred_twice_keeps_its_original_queue_timestamp(monkeypatch, tmp_path):
    from datetime import datetime, timedelta
    from src.retry_queue import load_deferred
    _seed_queue(tmp_path, [_offer(1)], age=timedelta(days=2))

    _run_handler(monkeypatch, tmp_path, [_offer(2)], score=_score_nothing)

    entries = {e.offer.link: e.queued_at for e in load_deferred(_queue_path(tmp_path))}
    assert set(entries) == {"https://x/1", "https://x/2"}
    assert entries["https://x/1"] < datetime.now() - timedelta(days=1, hours=12)
    assert entries["https://x/2"] > datetime.now() - timedelta(minutes=5)


def test_a_requeued_offer_scraped_again_today_keeps_its_first_deferral_clock(monkeypatch, tmp_path):
    # Today's scrape returns an already-queued offer, so it is re-queued as
    # today's copy - but restarting its expiry clock would let a re-scraped
    # offer sit on the queue forever, which is what MAX_AGE_DAYS exists to stop.
    from datetime import datetime, timedelta
    from src.retry_queue import MAX_AGE_DAYS, load_deferred
    _seed_queue(tmp_path, [_offer(1)], age=timedelta(days=2))

    _run_handler(monkeypatch, tmp_path, [_offer(1)], score=_score_nothing)

    entries = {e.offer.link: e.queued_at for e in load_deferred(_queue_path(tmp_path))}
    assert set(entries) == {"https://x/1"}
    assert entries["https://x/1"] < datetime.now() - timedelta(days=1, hours=12)
    later = datetime.now() + timedelta(days=MAX_AGE_DAYS - 1)
    assert load_deferred(_queue_path(tmp_path), now=later) == []


def test_a_timezone_aware_queue_entry_does_not_take_down_the_run(monkeypatch, tmp_path):
    # A hand-edited or externally written line can carry a UTC offset. Comparing
    # it against a naive cutoff used to raise TypeError out of handler and into
    # run_tier_with_retry, re-running the whole tier four times.
    from datetime import datetime, timedelta, timezone
    from src.retry_queue import QueueEntry
    aware = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=5))) - timedelta(hours=1)
    entry = QueueEntry.model_construct(queued_at=aware, offer=_offer(1))
    with open(_queue_path(tmp_path), "w") as f:
        f.write(entry.model_dump_json() + "\n")

    calls, _ = _run_handler(monkeypatch, tmp_path, [_offer(2)])

    assert [o.link for o in calls["score_input"]] == ["https://x/1", "https://x/2"]


def test_expired_queue_entries_are_not_rescored(monkeypatch, tmp_path):
    from datetime import timedelta
    from src.retry_queue import MAX_AGE_DAYS, load_deferred
    _seed_queue(tmp_path, [_offer(1)], age=timedelta(days=MAX_AGE_DAYS, hours=1))

    calls, _ = _run_handler(monkeypatch, tmp_path, [_offer(2)])

    assert [o.link for o in calls["score_input"]] == ["https://x/2"]
    assert load_deferred(_queue_path(tmp_path)) == []


def test_queued_offers_keep_their_verdict_and_rejected_ones_stay_marked_seen(monkeypatch, tmp_path):
    """With verification on: rejected offers are handled (marked seen), the
    survivor scoring never reached is deferred with its verdict intact, so the
    next run does not pay Groq to re-verify it."""
    from src.dedup import filter_new
    from src.retry_queue import load_deferred
    fetched = [_offer(0), _offer(1), _offer(2)]

    def fake_verify(offers, require_italy_eligibility, groq_api_key):
        offers[0].remote_verdict = "rejected"
        for o in offers[1:]:
            o.remote_verdict = "confirmed"
        return offers, dict(_ZERO_USAGE)

    calls, send_summary = _run_handler(monkeypatch, tmp_path, fetched,
                                       score=_score_first_n(1), remote_check=True,
                                       verify=fake_verify)

    assert [o.link for o in calls["score_input"]] == ["https://x/1", "https://x/2"]
    assert [o.link for o in filter_new(fetched, _seen_path(tmp_path))] == ["https://x/2"]
    queue = load_deferred(_queue_path(tmp_path))
    assert [e.offer.link for e in queue] == ["https://x/2"]
    assert queue[0].offer.remote_verdict == "confirmed"
    assert send_summary.call_args.kwargs["deferred_count"] == 1


def test_queued_offers_are_renumbered_so_their_ids_cannot_collide(monkeypatch, tmp_path):
    # src/scorer.py keys results by offer id; a queued offer carrying the id it
    # had on the run that fetched it would cross-wire today's scores.
    _seed_queue(tmp_path, [_offer(7), _offer(8)])

    calls, _ = _run_handler(monkeypatch, tmp_path, [_offer(0), _offer(1)])

    assert [o.link for o in calls["score_input"]] == [
        "https://x/7", "https://x/8", "https://x/0", "https://x/1",
    ]
    assert [o.id for o in calls["score_input"]] == [0, 1, 2, 3]


def test_a_queued_offer_rejected_by_todays_verification_is_dropped(monkeypatch, tmp_path):
    """Today's verdict is the newer one. If the fresh copy of a queued offer is
    rejected as not full-remote, the stale queued copy must not slip into
    scoring behind that verdict, and it must not linger in the queue file."""
    from src.retry_queue import load_deferred
    _seed_queue(tmp_path, [_offer(1)])

    def fake_verify(offers, require_italy_eligibility, groq_api_key):
        for o in offers:
            o.remote_verdict = "rejected"
        return offers, dict(_ZERO_USAGE)

    calls, _ = _run_handler(monkeypatch, tmp_path, [_offer(1), _offer(2)],
                            remote_check=True, verify=fake_verify)

    assert calls["score_input"] == []
    assert load_deferred(_queue_path(tmp_path)) == []



# --- an unguarded summary call must not re-run the tier --------------------


def test_a_telegram_outage_does_not_re_run_the_tier(monkeypatch, tmp_path):
    """Every piece of real work is done before send_summary runs, so a
    ConnectionError there must degrade the notification, not re-trigger
    run_tier_with_retry's full re-scrape/re-verify/re-score (4x quota)."""
    import requests as _requests
    config_path = _config_with(tmp_path, monkeypatch)
    calls = {"fetch": 0, "score": 0}

    def fake_fetch(**kwargs):
        calls["fetch"] += 1
        return [_offer(1)]

    def fake_score(offers, **kwargs):
        calls["score"] += 1
        return _score_all(offers)

    import main
    _stub_common_pipeline(monkeypatch)
    monkeypatch.setattr("main.fetch_offers", fake_fetch)
    monkeypatch.setattr("main.filter_by_language", lambda offers: offers)
    monkeypatch.setattr("main.score_offers", fake_score)
    monkeypatch.setattr("main._USAGE_LOG_PATH", str(tmp_path / "usage_log.jsonl"))
    monkeypatch.setattr(
        "main.send_summary",
        MagicMock(side_effect=_requests.ConnectionError("Telegram API unreachable")),
    )
    mock_send_message = MagicMock()
    monkeypatch.setattr("main.send_message", mock_send_message)

    sleeps = []
    main.run_tier_with_retry(str(config_path), sleep=sleeps.append)

    assert calls == {"fetch": 1, "score": 1}
    assert sleeps == []
    text = mock_send_message.call_args.args[0]
    assert "Tier 1" in text
    assert "ConnectionError" in text


def test_handler_survives_the_summary_failure_notice_also_failing(monkeypatch, tmp_path, capsys):
    import requests as _requests
    config_path = _config_with(tmp_path, monkeypatch)

    import main
    _stub_common_pipeline(monkeypatch)
    monkeypatch.setattr("main.fetch_offers", lambda **kwargs: [_offer(1)])
    monkeypatch.setattr("main.filter_by_language", lambda offers: offers)
    monkeypatch.setattr("main.score_offers", _score_all)
    monkeypatch.setattr("main._USAGE_LOG_PATH", str(tmp_path / "usage_log.jsonl"))
    monkeypatch.setattr(
        "main.send_summary",
        MagicMock(side_effect=_requests.ConnectionError("Telegram API unreachable")),
    )
    monkeypatch.setattr(
        "main.send_message",
        MagicMock(side_effect=_requests.ConnectionError("still down")),
    )

    main.handler({}, None, config_path=str(config_path))  # must not raise

    assert "Failed to send summary failure notification" in capsys.readouterr().out
