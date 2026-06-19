from unittest.mock import MagicMock, patch
from src.models import JobOffer, ScoredOffer
from src.config import AppConfig, SearchConfig, ScoringConfig, TelegramConfig


def _mock_config():
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
    mock_write_notes.assert_called_once_with(scored_offers, "/output", 8, 1)
    mock_write_digest.assert_called_once_with(scored_offers, "/output", 8, tier=1)
    mock_send.assert_called_once()
    mock_mark.assert_called_once_with(new_offers, "/data/seen.txt")


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
