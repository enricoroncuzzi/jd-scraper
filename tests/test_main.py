from unittest.mock import MagicMock, patch
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
    mock_write_digest.assert_called_once_with(scored_offers, "/output", 8, tier=1)
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

    monkeypatch.setattr("main.load_config", mock_load_config)
    monkeypatch.setattr("main.fetch_offers", mock_fetch)
    monkeypatch.setattr("main.filter_by_language", mock_lang_filter)
    monkeypatch.setattr("main.filter_new", mock_filter)
    monkeypatch.setattr("main.score_offers", mock_score)

    import main
    main.handler({}, None)

    mock_score.assert_not_called()


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
