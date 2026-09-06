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

META_ONLY_HTML = """
<html>
<head><meta name="description" content="Exciting AI role at a top startup."></head>
<body></body>
</html>
"""

PARAGRAPH_HTML = """
<html><body>
<p>We are hiring an experienced AI Engineer to join our growing team and build production LLM systems.</p>
</body></html>
"""

EMPTY_PAGE_HTML = "<html><body></body></html>"


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
    monkeypatch.setattr("src.scraper.random.uniform", lambda a, b: a)

    offers = fetch_offers(["AI Engineer"], "Europe", "r86400")

    assert len(offers) == 1
    assert offers[0].title == "AI Engineer"
    assert offers[0].company == "Some Company"
    assert offers[0].location == "Milan, Italy"
    assert "linkedin.com/jobs/view/1234567890" in offers[0].link
    assert "RAG pipelines" in offers[0].description
    assert offers[0].description_status == "ok"


def test_fetch_offers_strips_link_query_params(monkeypatch):
    monkeypatch.setattr("src.scraper.requests.get", _make_mock_get(SEARCH_HTML, DESCRIPTION_HTML))
    monkeypatch.setattr("src.scraper.time.sleep", lambda _: None)
    monkeypatch.setattr("src.scraper.random.uniform", lambda a, b: a)

    offers = fetch_offers(["AI Engineer"], "Europe", "r86400")

    assert "?" not in offers[0].link


def test_fetch_offers_raises_on_search_non_200(monkeypatch):
    def mock_get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 429
        return resp
    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda _: None)
    monkeypatch.setattr("src.scraper.random.uniform", lambda a, b: a)

    with pytest.raises(RuntimeError, match="429"):
        fetch_offers(["AI Engineer"], "Europe", "r86400")


def test_fetch_offers_partial_description_on_non_retriable_status(monkeypatch):
    monkeypatch.setattr("src.scraper.requests.get",
                        _make_mock_get(SEARCH_HTML, EMPTY_PAGE_HTML, description_status=404))
    monkeypatch.setattr("src.scraper.time.sleep", lambda _: None)
    monkeypatch.setattr("src.scraper.random.uniform", lambda a, b: a)

    offers = fetch_offers(["AI Engineer"], "Europe", "r86400")

    assert len(offers) == 1
    assert offers[0].description == "AI Engineer at Some Company"
    assert offers[0].description_status == "partial"


def test_fetch_offers_partial_description_from_meta_tag(monkeypatch):
    monkeypatch.setattr("src.scraper.requests.get",
                        _make_mock_get(SEARCH_HTML, META_ONLY_HTML))
    monkeypatch.setattr("src.scraper.time.sleep", lambda _: None)
    monkeypatch.setattr("src.scraper.random.uniform", lambda a, b: a)

    offers = fetch_offers(["AI Engineer"], "Europe", "r86400")

    assert len(offers) == 1
    assert "Exciting AI role" in offers[0].description
    assert offers[0].description_status == "partial"


def test_fetch_offers_partial_description_from_paragraph(monkeypatch):
    monkeypatch.setattr("src.scraper.requests.get",
                        _make_mock_get(SEARCH_HTML, PARAGRAPH_HTML))
    monkeypatch.setattr("src.scraper.time.sleep", lambda _: None)
    monkeypatch.setattr("src.scraper.random.uniform", lambda a, b: a)

    offers = fetch_offers(["AI Engineer"], "Europe", "r86400")

    assert len(offers) == 1
    assert "LLM systems" in offers[0].description
    assert offers[0].description_status == "partial"


def test_fetch_offers_partial_description_falls_back_to_title_company(monkeypatch):
    monkeypatch.setattr("src.scraper.requests.get",
                        _make_mock_get(SEARCH_HTML, EMPTY_PAGE_HTML))
    monkeypatch.setattr("src.scraper.time.sleep", lambda _: None)
    monkeypatch.setattr("src.scraper.random.uniform", lambda a, b: a)

    offers = fetch_offers(["AI Engineer"], "Europe", "r86400")

    assert len(offers) == 1
    assert offers[0].description == "AI Engineer at Some Company"
    assert offers[0].description_status == "partial"


def test_fetch_offers_failed_description_when_429_exhausted(monkeypatch):
    call_count = {"n": 0}

    def mock_get(url, **kwargs):
        resp = MagicMock()
        if "seeMoreJobPostings" in url:
            resp.status_code = 200
            resp.text = SEARCH_HTML
        else:
            call_count["n"] += 1
            resp.status_code = 429
        return resp

    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda _: None)
    monkeypatch.setattr("src.scraper.random.uniform", lambda a, b: a)

    offers = fetch_offers(["AI Engineer"], "Europe", "r86400")

    assert len(offers) == 1
    assert offers[0].description == ""
    assert offers[0].description_status == "failed"


def test_fetch_offers_skips_cards_without_title_or_link(monkeypatch):
    html_no_link = """
    <ul>
      <li><div><h3 class="base-search-card__title">No Link Here</h3></div></li>
    </ul>
    """
    monkeypatch.setattr("src.scraper.requests.get", _make_mock_get(html_no_link, ""))
    monkeypatch.setattr("src.scraper.time.sleep", lambda _: None)
    monkeypatch.setattr("src.scraper.random.uniform", lambda a, b: a)

    offers = fetch_offers(["AI Engineer"], "Europe", "r86400")
    assert offers == []


def test_fetch_offers_multiple_roles_merged(monkeypatch):
    # Keyed by role and by `start` (not by call order) so pagination's extra
    # terminating request per role doesn't disturb which HTML each role sees.
    call_count = {"n": 0}

    def mock_get(url, **kwargs):
        resp = MagicMock()
        if "seeMoreJobPostings" in url:
            resp.status_code = 200
            params = kwargs.get("params", {})
            call_count["n"] += 1
            if params.get("start", 0) > 0:
                resp.text = EMPTY_PAGE_HTML
            elif params["keywords"] == "AI Engineer":
                resp.text = SEARCH_HTML.replace("1234567890", "111")
            else:
                resp.text = SEARCH_HTML.replace("1234567890", "222").replace("AI Engineer", "ML Engineer")
        else:
            resp.status_code = 200
            resp.text = DESCRIPTION_HTML
        return resp

    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda _: None)
    monkeypatch.setattr("src.scraper.random.uniform", lambda a, b: a)

    offers = fetch_offers(["AI Engineer", "ML Engineer"], "Europe", "r86400")
    assert len(offers) == 2
    # 2 search requests per role: one page with a card, one empty page that
    # ends pagination.
    assert call_count["n"] == 4


def test_fetch_offers_deduplicates_across_roles(monkeypatch):
    monkeypatch.setattr("src.scraper.requests.get", _make_mock_get(SEARCH_HTML, DESCRIPTION_HTML))
    monkeypatch.setattr("src.scraper.time.sleep", lambda _: None)
    monkeypatch.setattr("src.scraper.random.uniform", lambda a, b: a)

    offers = fetch_offers(["AI Engineer", "ML Engineer"], "Europe", "r86400")
    assert len(offers) == 1


def test_fetch_offers_stores_work_mode_on_offer(monkeypatch):
    monkeypatch.setattr("src.scraper.requests.get", _make_mock_get(SEARCH_HTML, DESCRIPTION_HTML))
    monkeypatch.setattr("src.scraper.time.sleep", lambda _: None)
    monkeypatch.setattr("src.scraper.random.uniform", lambda a, b: a)

    offers = fetch_offers(["AI Engineer"], "Europe", "r86400", work_modes=["remote"])
    assert offers[0].work_mode == "remote"


def test_fetch_offers_passes_work_mode_remote(monkeypatch):
    captured = {}

    def mock_get(url, **kwargs):
        resp = MagicMock()
        if "seeMoreJobPostings" in url:
            captured["params"] = kwargs.get("params", {})
            resp.status_code = 200
            resp.text = "<ul></ul>"
        else:
            resp.status_code = 200
            resp.text = ""
        return resp

    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda _: None)
    monkeypatch.setattr("src.scraper.random.uniform", lambda a, b: a)

    fetch_offers(["AI Engineer"], "Europe", "r86400", work_modes=["remote"])
    assert captured["params"]["f_WT"] == "2"


def test_fetch_offers_passes_work_mode_hybrid(monkeypatch):
    captured = {}

    def mock_get(url, **kwargs):
        resp = MagicMock()
        if "seeMoreJobPostings" in url:
            captured["params"] = kwargs.get("params", {})
            resp.status_code = 200
            resp.text = "<ul></ul>"
        else:
            resp.status_code = 200
            resp.text = ""
        return resp

    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda _: None)
    monkeypatch.setattr("src.scraper.random.uniform", lambda a, b: a)

    fetch_offers(["AI Engineer"], "Europe", "r86400", work_modes=["hybrid"])
    assert captured["params"]["f_WT"] == "3"


def test_fetch_offers_both_work_modes_makes_two_calls_per_role(monkeypatch):
    search_call_count = {"n": 0}

    def mock_get(url, **kwargs):
        resp = MagicMock()
        if "seeMoreJobPostings" in url:
            search_call_count["n"] += 1
            resp.status_code = 200
            resp.text = "<ul></ul>"
        else:
            resp.status_code = 200
            resp.text = ""
        return resp

    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda _: None)
    monkeypatch.setattr("src.scraper.random.uniform", lambda a, b: a)

    fetch_offers(["AI Engineer"], "Europe", "r86400", work_modes=["remote", "hybrid"])
    assert search_call_count["n"] == 2


def _make_location_capturing_mock_get(captured_locations):
    def mock_get(url, **kwargs):
        resp = MagicMock()
        if "seeMoreJobPostings" in url:
            captured_locations.append(kwargs["params"]["location"])
            resp.status_code = 200
            resp.text = "<ul></ul>"
        else:
            resp.status_code = 200
            resp.text = ""
        return resp
    return mock_get


def test_fetch_offers_queries_each_country_when_provided(monkeypatch):
    captured_locations = []

    monkeypatch.setattr("src.scraper.requests.get", _make_location_capturing_mock_get(captured_locations))
    monkeypatch.setattr("src.scraper.time.sleep", lambda _: None)
    monkeypatch.setattr("src.scraper.random.uniform", lambda a, b: a)

    fetch_offers(["AI Engineer"], "Europe", "r86400", countries=["Italy", "Spain"])
    assert captured_locations == ["Italy", "Spain"]


def test_fetch_offers_falls_back_to_location_when_no_countries(monkeypatch):
    captured_locations = []

    monkeypatch.setattr("src.scraper.requests.get", _make_location_capturing_mock_get(captured_locations))
    monkeypatch.setattr("src.scraper.time.sleep", lambda _: None)
    monkeypatch.setattr("src.scraper.random.uniform", lambda a, b: a)

    fetch_offers(["AI Engineer"], "Europe", "r86400")
    assert captured_locations == ["Europe"]


def test_fetch_offers_deduplicates_across_countries(monkeypatch):
    monkeypatch.setattr("src.scraper.requests.get", _make_mock_get(SEARCH_HTML, DESCRIPTION_HTML))
    monkeypatch.setattr("src.scraper.time.sleep", lambda _: None)
    monkeypatch.setattr("src.scraper.random.uniform", lambda a, b: a)

    offers = fetch_offers(["AI Engineer"], "Europe", "r86400", countries=["Italy", "Spain"])
    assert len(offers) == 1


def _search_html(cards):
    """cards: list of (job_id, title, location) tuples."""
    items = "".join(
        f"""
  <li>
    <div class="base-search-card">
      <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/{job_id}?ref=x">{title}</a>
      <h3 class="base-search-card__title">{title}</h3>
      <h4 class="base-search-card__subtitle"><a href="#">Some Company</a></h4>
      <span class="job-search-card__location">{location}</span>
    </div>
  </li>"""
        for job_id, title, location in cards
    )
    return f"<ul>{items}</ul>"


def _paging_mock_get(pages, description_html=DESCRIPTION_HTML):
    """pages: dict mapping the `start` param to that page's search HTML."""
    calls = {"search_starts": [], "description_urls": []}

    def mock_get(url, **kwargs):
        response = MagicMock()
        response.status_code = 200
        if "seeMoreJobPostings" in url:
            start = kwargs.get("params", {}).get("start", 0)
            calls["search_starts"].append(start)
            response.text = pages.get(start, EMPTY_PAGE_HTML)
        else:
            calls["description_urls"].append(url)
            response.text = description_html
        return response

    return mock_get, calls


def test_pagination_walks_pages_until_empty(monkeypatch):
    pages = {
        0: _search_html([(1, "AI Engineer", "Berlin, Germany"), (2, "ML Engineer", "Paris, France")]),
        25: _search_html([(3, "Data Scientist", "Madrid, Spain")]),
        50: EMPTY_PAGE_HTML,
    }
    mock_get, calls = _paging_mock_get(pages)
    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda s: None)

    offers = fetch_offers(["AI Engineer"], "Europe", "r86400")

    assert len(offers) == 3
    assert calls["search_starts"][:3] == [0, 25, 50]


def test_pagination_stops_when_a_page_adds_no_new_links(monkeypatch):
    page = _search_html([(1, "AI Engineer", "Berlin, Germany")])
    pages = {0: page, 25: page, 50: page}
    mock_get, calls = _paging_mock_get(pages)
    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda s: None)

    offers = fetch_offers(["AI Engineer"], "Europe", "r86400")

    assert len(offers) == 1
    assert calls["search_starts"] == [0, 25]


def test_pagination_honours_the_page_cap(monkeypatch):
    pages = {
        start: _search_html([(start + 1, "AI Engineer", "Berlin, Germany")])
        for start in range(0, 25 * 40, 25)
    }
    mock_get, calls = _paging_mock_get(pages)
    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda s: None)

    fetch_offers(["AI Engineer"], "Europe", "r86400")

    from src.scraper import _MAX_PAGES_PER_QUERY
    assert len(calls["search_starts"]) == _MAX_PAGES_PER_QUERY


def test_scope_filter_discards_out_of_scope_cards(monkeypatch):
    from src.tier_scope import TIER3_ALLOWED_COUNTRIES

    pages = {0: _search_html([
        (1, "In Scope", "Berlin, Germany"),
        (2, "Out Of Scope", "Milan, Italy"),
    ])}
    mock_get, calls = _paging_mock_get(pages)
    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda s: None)

    offers = fetch_offers(
        ["AI Engineer"], "Europe", "r86400",
        allowed_countries=TIER3_ALLOWED_COUNTRIES,
    )

    assert [o.title for o in offers] == ["In Scope"]


def test_scope_filter_spends_no_description_fetch_on_a_discarded_card(monkeypatch):
    from src.tier_scope import TIER3_ALLOWED_COUNTRIES

    pages = {0: _search_html([
        (1, "In Scope", "Berlin, Germany"),
        (2, "Out Of Scope", "Milan, Italy"),
    ])}
    mock_get, calls = _paging_mock_get(pages)
    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda s: None)

    fetch_offers(
        ["AI Engineer"], "Europe", "r86400",
        allowed_countries=TIER3_ALLOWED_COUNTRIES,
    )

    assert len(calls["description_urls"]) == 1
    assert "1" in calls["description_urls"][0]
