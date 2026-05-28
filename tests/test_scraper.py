import pytest
from unittest.mock import MagicMock
from src.scraper import fetch_offers

SEARCH_HTML = """
<ul>
  <li>
    <div class="base-search-card">
      <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/1234567890?extra=stuff">
        AI Engineer
      </a>
      <h3 class="base-search-card__title">AI Engineer</h3>
      <h4 class="base-search-card__subtitle">
        <a href="#">Some Company</a>
      </h4>
      <span class="job-search-card__location">Milan, Italy</span>
    </div>
  </li>
</ul>
"""

DESCRIPTION_HTML = """
<div class="show-more-less-html__markup">
  We are looking for an AI Engineer to build RAG pipelines.
</div>
"""


def _make_mock_get(search_html, description_html, description_status=200):
    def mock_get(url, **kwargs):
        resp = MagicMock()
        if "seeMoreJobPostings" in url:
            resp.status_code = 200
            resp.text = search_html
        else:
            resp.status_code = description_status
            resp.text = description_html
        return resp
    return mock_get


def test_fetch_offers_parses_cards(monkeypatch):
    monkeypatch.setattr("src.scraper.requests.get", _make_mock_get(SEARCH_HTML, DESCRIPTION_HTML))
    monkeypatch.setattr("src.scraper.time.sleep", lambda _: None)

    offers = fetch_offers("AI Engineer", "Europe", "r86400")

    assert len(offers) == 1
    assert offers[0].title == "AI Engineer"
    assert offers[0].company == "Some Company"
    assert offers[0].location == "Milan, Italy"
    assert "linkedin.com/jobs/view/1234567890" in offers[0].link
    assert "RAG pipelines" in offers[0].description


def test_fetch_offers_strips_link_query_params(monkeypatch):
    monkeypatch.setattr("src.scraper.requests.get", _make_mock_get(SEARCH_HTML, DESCRIPTION_HTML))
    monkeypatch.setattr("src.scraper.time.sleep", lambda _: None)

    offers = fetch_offers("AI Engineer", "Europe", "r86400")

    assert "?" not in offers[0].link


def test_fetch_offers_raises_on_search_non_200(monkeypatch):
    def mock_get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 429
        return resp
    monkeypatch.setattr("src.scraper.requests.get", mock_get)

    with pytest.raises(RuntimeError, match="429"):
        fetch_offers("AI Engineer", "Europe", "r86400")


def test_fetch_offers_sets_empty_description_on_fetch_failure(monkeypatch):
    monkeypatch.setattr("src.scraper.requests.get",
                        _make_mock_get(SEARCH_HTML, "", description_status=404))
    monkeypatch.setattr("src.scraper.time.sleep", lambda _: None)

    offers = fetch_offers("AI Engineer", "Europe", "r86400")

    assert len(offers) == 1
    assert offers[0].description == ""


def test_fetch_offers_skips_cards_without_title_or_link(monkeypatch):
    html_no_link = """
    <ul>
      <li><div><h3 class="base-search-card__title">No Link Here</h3></div></li>
    </ul>
    """
    monkeypatch.setattr("src.scraper.requests.get", _make_mock_get(html_no_link, ""))
    monkeypatch.setattr("src.scraper.time.sleep", lambda _: None)

    offers = fetch_offers("AI Engineer", "Europe", "r86400")
    assert offers == []
