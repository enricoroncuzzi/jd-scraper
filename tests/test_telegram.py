from unittest.mock import MagicMock
from src.models import ScoredOffer
from src.telegram import send_summary


def _offer(id, title, score):
    return ScoredOffer(
        id=id, title=title, company="Acme", location="EU",
        link=f"https://li.com/{id}", score=score,
        comment="ok", summary="summary here"
    )


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


def test_send_summary_logs_on_failure(monkeypatch, capsys):
    mock_response = MagicMock()
    mock_response.ok = False
    mock_response.status_code = 400
    mock_response.text = "Bad Request"
    monkeypatch.setattr("src.telegram.requests.post", MagicMock(return_value=mock_response))

    send_summary([], threshold=8, greeting="Hey!", token="t", chat_id="c")

    captured = capsys.readouterr()
    assert "[telegram]" in captured.out
