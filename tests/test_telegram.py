from unittest.mock import MagicMock
from src.models import ScoredOffer
from src.telegram import send_summary


def _offer(id, title, score):
    return ScoredOffer(
        id=id, title=title, company="Acme", location="EU",
        link=f"https://li.com/{id}", score=score,
        comment="ok", summary="summary here"
    )


def _scored(score, verdict="not_checked", title="Role"):
    return ScoredOffer(
        id=hash(title) % 1000000, title=title, company="Acme", location="EU",
        link=f"https://li.com/{title}", score=score,
        comment="ok", summary="summary here", remote_verdict=verdict,
    )


def _capture(monkeypatch):
    mock_response = MagicMock()
    mock_response.ok = True
    mock_post = MagicMock(return_value=mock_response)
    monkeypatch.setattr("src.telegram.requests.post", mock_post)

    class _Sent:
        def __getitem__(self, key):
            return mock_post.call_args[1]["json"][key]

    return _Sent()


def test_send_summary_posts_to_telegram(monkeypatch):
    mock_response = MagicMock()
    mock_response.ok = True
    mock_post = MagicMock(return_value=mock_response)
    monkeypatch.setattr("src.telegram.requests.post", mock_post)

    offers = [_offer(0, "AI Engineer", 9), _offer(1, "Java Dev", 4)]
    send_summary(offers, threshold=8, greeting="Hey Enrico!",
                 token="test-token", chat_id="123456")

    mock_post.assert_called_once()
    url = mock_post.call_args[0][0]
    assert "test-token" in url
    payload = mock_post.call_args[1]["json"]
    assert payload["chat_id"] == "123456"
    assert "Hey Enrico!" in payload["text"]
    assert "AI Engineer" in payload["text"]


def test_send_summary_no_offers(monkeypatch):
    mock_response = MagicMock()
    mock_response.ok = True
    mock_post = MagicMock(return_value=mock_response)
    monkeypatch.setattr("src.telegram.requests.post", mock_post)

    send_summary([], threshold=8, greeting="Hey!", token="t", chat_id="c")

    mock_post.assert_called_once()
    assert "No new offers" in mock_post.call_args[1]["json"]["text"]


def test_send_summary_message_fits_telegram_limit(monkeypatch):
    mock_response = MagicMock()
    mock_response.ok = True
    mock_post = MagicMock(return_value=mock_response)
    monkeypatch.setattr("src.telegram.requests.post", mock_post)

    # 60 high-score offers - simulates the real failure case
    offers = [_offer(i, f"AI Engineer Role {i}", 9) for i in range(60)]
    send_summary(offers, threshold=8, greeting="Hey Enrico!", token="t", chat_id="c")

    text = mock_post.call_args[1]["json"]["text"]
    assert len(text) <= 4096


def test_send_summary_shows_top5_high_score_with_remainder_note(monkeypatch):
    mock_response = MagicMock()
    mock_response.ok = True
    mock_post = MagicMock(return_value=mock_response)
    monkeypatch.setattr("src.telegram.requests.post", mock_post)

    offers = [_offer(i, f"Role {i}", 9) for i in range(8)]
    send_summary(offers, threshold=8, greeting="Hey!", token="t", chat_id="c")

    text = mock_post.call_args[1]["json"]["text"]
    assert "Role 0" in text  # top 5 shown
    assert "Role 4" in text
    assert "Role 5" not in text  # 6th not shown in detail
    assert "+3 more" in text  # remainder noted


def test_send_summary_logs_on_failure(monkeypatch, capsys):
    mock_response = MagicMock()
    mock_response.ok = False
    mock_response.status_code = 400
    mock_response.text = "Bad Request"
    monkeypatch.setattr("src.telegram.requests.post", MagicMock(return_value=mock_response))

    send_summary([], threshold=8, greeting="Hey!", token="t", chat_id="c")

    captured = capsys.readouterr()
    assert "[telegram]" in captured.out


def test_summary_separates_confirmed_from_unconfirmed(monkeypatch):
    sent = _capture(monkeypatch)
    offers = [
        _scored(9, verdict="confirmed", title="Confirmed Role"),
        _scored(9, verdict="unconfirmed", title="Unsure Role"),
    ]
    send_summary(offers, 8, "Hey!", "t", "1", verification_enabled=True)

    text = sent["text"]
    assert "Confirmed full-remote" in text
    assert "Remote not confirmed" in text
    assert text.index("Confirmed Role") < text.index("Unsure Role")


def test_summary_has_no_sections_when_verification_is_off(monkeypatch):
    sent = _capture(monkeypatch)
    send_summary([_scored(9, verdict="not_checked")], 8, "Hey!", "t", "1",
                 verification_enabled=False)
    assert "Confirmed full-remote" not in sent["text"]


def test_degraded_verification_is_announced(monkeypatch):
    sent = _capture(monkeypatch)
    send_summary([_scored(9, verdict="unconfirmed")], 8, "Hey!", "t", "1",
                 verification_enabled=True, verification_degraded=True)
    assert "verification did not run" in sent["text"].lower()
