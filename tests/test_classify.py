from unittest.mock import MagicMock, patch
import requests
from src.autoapply.classify import (
    classify_channel,
    CHANNEL_LINKEDIN_EASY_APPLY,
    CHANNEL_EXTERNAL_ATS,
    CHANNEL_EMAIL_APPLY,
    CHANNEL_UNKNOWN,
)


def _mock_response(url, text, status_code=200):
    resp = MagicMock()
    resp.url = url
    resp.text = text
    resp.status_code = status_code
    return resp


def test_classify_channel_easy_apply_marker():
    html = '<html><body><button class="jobs-apply-button">Easy Apply</button></body></html>'
    resp = _mock_response("https://www.linkedin.com/jobs/view/123", html)
    with patch("src.autoapply.classify.requests.get", return_value=resp):
        assert classify_channel("https://www.linkedin.com/jobs/view/123") == CHANNEL_LINKEDIN_EASY_APPLY


def test_classify_channel_offsite_apply_link_is_external_ats():
    html = (
        '<html><body>'
        '<a href="https://boards.greenhouse.io/acme/jobs/1" '
        'data-tracking-control-name="public_jobs_apply-link-offsite">Apply</a>'
        '</body></html>'
    )
    resp = _mock_response("https://www.linkedin.com/jobs/view/123", html)
    with patch("src.autoapply.classify.requests.get", return_value=resp):
        assert classify_channel("https://www.linkedin.com/jobs/view/123") == CHANNEL_EXTERNAL_ATS


def test_classify_channel_generic_offsite_text_fallback():
    html = '<html><body><a href="https://jobs.acme.com/apply/1">Apply now</a></body></html>'
    resp = _mock_response("https://www.linkedin.com/jobs/view/123", html)
    with patch("src.autoapply.classify.requests.get", return_value=resp):
        assert classify_channel("https://www.linkedin.com/jobs/view/123") == CHANNEL_EXTERNAL_ATS


def test_classify_channel_mailto_is_email_apply():
    html = '<html><body><a href="mailto:jobs@acme.com">Apply via email</a></body></html>'
    resp = _mock_response("https://www.linkedin.com/jobs/view/123", html)
    with patch("src.autoapply.classify.requests.get", return_value=resp):
        assert classify_channel("https://www.linkedin.com/jobs/view/123") == CHANNEL_EMAIL_APPLY


def test_classify_channel_no_markers_is_unknown():
    html = "<html><body><p>Nothing useful here</p></body></html>"
    resp = _mock_response("https://www.linkedin.com/jobs/view/123", html)
    with patch("src.autoapply.classify.requests.get", return_value=resp):
        assert classify_channel("https://www.linkedin.com/jobs/view/123") == CHANNEL_UNKNOWN


def test_classify_channel_direct_redirect_to_known_ats():
    resp = _mock_response("https://boards.greenhouse.io/acme/jobs/1", "<html></html>")
    with patch("src.autoapply.classify.requests.get", return_value=resp):
        assert classify_channel("https://www.linkedin.com/jobs/view/123") == CHANNEL_EXTERNAL_ATS


def test_classify_channel_network_error_is_unknown():
    with patch("src.autoapply.classify.requests.get", side_effect=requests.RequestException("boom")):
        assert classify_channel("https://www.linkedin.com/jobs/view/123") == CHANNEL_UNKNOWN


def test_classify_channel_non_200_is_unknown():
    resp = _mock_response("https://www.linkedin.com/jobs/view/123", "", status_code=404)
    with patch("src.autoapply.classify.requests.get", return_value=resp):
        assert classify_channel("https://www.linkedin.com/jobs/view/123") == CHANNEL_UNKNOWN


def test_classify_channel_only_calls_requests_get():
    html = '<html><body><button class="jobs-apply-button">Easy Apply</button></body></html>'
    resp = _mock_response("https://www.linkedin.com/jobs/view/123", html)
    with patch("src.autoapply.classify.requests.get", return_value=resp) as mock_get, \
            patch("src.autoapply.classify.requests.post") as mock_post:
        result = classify_channel("https://www.linkedin.com/jobs/view/123")
    assert result == CHANNEL_LINKEDIN_EASY_APPLY
    mock_get.assert_called_once()
    args, kwargs = mock_get.call_args
    assert kwargs.get("allow_redirects") is True
    mock_post.assert_not_called()


def test_classify_channel_returns_without_raising_for_malformed_html():
    resp = _mock_response("https://www.linkedin.com/jobs/view/123", "<html><body><a href></body>")
    with patch("src.autoapply.classify.requests.get", return_value=resp):
        assert classify_channel("https://www.linkedin.com/jobs/view/123") == CHANNEL_UNKNOWN
