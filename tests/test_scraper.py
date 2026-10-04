import pytest
from unittest.mock import MagicMock, patch
from src import scraper
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


def test_fetch_offers_rate_limits_description_instead_of_scoring_fallback_text(monkeypatch):
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
    assert offers[0].description != "AI Engineer at Some Company"
    assert offers[0].description_status == "rate_limited"
    assert call_count["n"] == scraper._DESC_MAX_RETRIES


def test_fetch_offers_rate_limits_description_when_503_or_504_exhausted(monkeypatch):
    def mock_get(url, **kwargs):
        resp = MagicMock()
        if "seeMoreJobPostings" in url:
            resp.status_code = 200
            resp.text = SEARCH_HTML
        else:
            resp.status_code = 503
        return resp

    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda _: None)
    monkeypatch.setattr("src.scraper.random.uniform", lambda a, b: a)

    offers = fetch_offers(["AI Engineer"], "Europe", "r86400")

    assert offers[0].description == ""
    assert offers[0].description_status == "rate_limited"


def test_pacing_increases_after_a_429_and_resets_after_a_clean_description(monkeypatch):
    """The gap after a throttled description is longer than the normal gap.
    A later description that is not rate-limited brings the gap back down.
    Retry backoff sleeps stay at zero here so only the inter-request gap shows."""
    delays = []
    monkeypatch.setattr("src.scraper.time.sleep", lambda seconds: delays.append(seconds))
    monkeypatch.setattr("src.scraper.random.uniform", lambda low, high: low)
    monkeypatch.setattr("src.scraper._wait_with_jitter", lambda base, cap: 0)

    html = SEARCH_HTML.replace(
        "</ul>",
        """
      <li>
        <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/222">x</a>
        <h3 class="base-search-card__title">ML Engineer</h3>
        <h4 class="base-search-card__subtitle">Other Co</h4>
        <span class="job-search-card__location">Milan, Italy</span>
      </li>
    </ul>""",
    )
    description_statuses = iter([429, 200, 200])

    def mock_get(url, **kwargs):
        resp = MagicMock()
        if "seeMoreJobPostings" in url:
            resp.status_code = 200
            start = kwargs.get("params", {}).get("start", 0)
            resp.text = html if start == 0 else EMPTY_PAGE_HTML
        else:
            resp.status_code = next(description_statuses)
            resp.text = DESCRIPTION_HTML
        return resp

    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    offers = fetch_offers(["AI Engineer"], "Europe", "r86400")

    paced = [delay for delay in delays if delay]
    assert len(offers) == 2
    assert paced[0] == scraper._PACE_LOW * 2
    assert paced[0] > scraper._PACE_LOW
    assert paced[1] == scraper._PACE_LOW
    assert scraper._pace.factor == 1


def test_pace_growth_is_capped():
    scraper._pace.reset()
    try:
        for _ in range(30):
            scraper._pace.slow()
        assert scraper._PACE_LOW * scraper._pace.factor <= scraper._PACE_DELAY_CAP
        scraper._pace.relax()
        assert scraper._pace.factor == 1
    finally:
        scraper._pace.reset()


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
    """pages: dict mapping the `start` param to that page's search HTML, or
    (for a page that should return a hard HTTP error instead of a normal
    response) an int status code."""
    calls = {"search_starts": [], "description_urls": []}

    def mock_get(url, **kwargs):
        response = MagicMock()
        if "seeMoreJobPostings" in url:
            start = kwargs.get("params", {}).get("start", 0)
            calls["search_starts"].append(start)
            page = pages.get(start, EMPTY_PAGE_HTML)
            if isinstance(page, int):
                response.status_code = page
                response.text = ""
            else:
                response.status_code = 200
                response.text = page
        else:
            response.status_code = 200
            calls["description_urls"].append(url)
            response.text = description_html
        return response

    return mock_get, calls


def test_pagination_walks_pages_until_empty(monkeypatch):
    pages = {
        0: _search_html([(1, "AI Engineer", "Berlin, Germany"), (2, "ML Engineer", "Paris, France")]),
        2: _search_html([(3, "Data Scientist", "Madrid, Spain")]),
        3: EMPTY_PAGE_HTML,
    }
    mock_get, calls = _paging_mock_get(pages)
    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda s: None)

    offers = fetch_offers(["AI Engineer"], "Europe", "r86400")

    assert len(offers) == 3
    assert calls["search_starts"][:3] == [0, 2, 3]


def test_pagination_covers_every_position_the_endpoint_returns(monkeypatch):
    # The guest endpoint returns 10 cards per request and honours `start` as a
    # real offset. A constant stride larger than that (25, until 2026-09)
    # skipped positions 10-24, 35-49, ... so ~60% of the results inside the
    # page cap were never seen. Stepping by the cards actually returned must
    # read every position instead.
    pages = {
        start: _search_html([(start + n, "AI Engineer", "Berlin, Germany") for n in range(10)])
        for start in (0, 10, 20)
    }
    pages[30] = EMPTY_PAGE_HTML
    mock_get, calls = _paging_mock_get(pages)
    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda s: None)

    offers = fetch_offers(["AI Engineer"], "Europe", "r86400")

    assert calls["search_starts"] == [0, 10, 20, 30]
    assert len(offers) == 30
    assert {int(o.link.rsplit("/", 1)[1]) for o in offers} == set(range(30))


def test_pagination_stops_when_a_page_adds_no_new_links(monkeypatch):
    page = _search_html([(1, "AI Engineer", "Berlin, Germany")])
    pages = {0: page, 1: page, 2: page}
    mock_get, calls = _paging_mock_get(pages)
    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda s: None)

    offers = fetch_offers(["AI Engineer"], "Europe", "r86400")

    assert len(offers) == 1
    assert calls["search_starts"] == [0, 1]


def test_pagination_honours_the_page_cap(monkeypatch):
    pages = {
        start: _search_html([(start + 1, "AI Engineer", "Berlin, Germany")])
        for start in range(40)
    }
    mock_get, calls = _paging_mock_get(pages)
    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda s: None)

    fetch_offers(["AI Engineer"], "Europe", "r86400")

    from src.scraper import _MAX_PAGES_PER_QUERY
    assert len(calls["search_starts"]) == _MAX_PAGES_PER_QUERY


def test_pagination_treats_a_hard_error_after_page_0_as_end_of_results(monkeypatch):
    pages = {
        0: _search_html([(1, "AI Engineer", "Berlin, Germany"), (2, "ML Engineer", "Paris, France")]),
        2: 400,
    }
    mock_get, calls = _paging_mock_get(pages)
    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda s: None)
    monkeypatch.setattr("src.scraper.random.uniform", lambda a, b: a)

    offers = fetch_offers(["AI Engineer"], "Europe", "r86400")

    assert len(offers) == 2
    assert calls["search_starts"] == [0, 2]


def test_pagination_raises_when_retries_are_exhausted_after_page_0(monkeypatch):
    pages = {
        0: _search_html([(1, "AI Engineer", "Berlin, Germany")]),
        1: 429,
    }
    mock_get, calls = _paging_mock_get(pages)
    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda s: None)
    monkeypatch.setattr("src.scraper.random.uniform", lambda a, b: a)

    with pytest.raises(RuntimeError, match="429"):
        fetch_offers(["AI Engineer"], "Europe", "r86400")


def test_pagination_raises_when_network_errors_exhaust_retries_after_page_0(monkeypatch):
    import requests as _requests

    base_mock_get, calls = _paging_mock_get(
        {0: _search_html([(1, "AI Engineer", "Berlin, Germany")])}
    )

    def mock_get(url, **kwargs):
        if "seeMoreJobPostings" in url and kwargs.get("params", {}).get("start", 0) == 1:
            calls["search_starts"].append(1)
            raise _requests.ConnectionError("boom")
        return base_mock_get(url, **kwargs)

    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda s: None)
    monkeypatch.setattr("src.scraper.random.uniform", lambda a, b: a)

    with pytest.raises(RuntimeError, match="network error"):
        fetch_offers(["AI Engineer"], "Europe", "r86400")


def test_pagination_still_raises_on_a_hard_error_on_page_0(monkeypatch):
    pages = {0: 400}
    mock_get, calls = _paging_mock_get(pages)
    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda s: None)
    monkeypatch.setattr("src.scraper.random.uniform", lambda a, b: a)

    with pytest.raises(RuntimeError, match="400"):
        fetch_offers(["AI Engineer"], "Europe", "r86400")


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


# The endpoint's cards-per-request today. Fixtures use it to build page
# offsets; src/scraper.py must NOT assume it (it steps by what comes back).
_ENDPOINT_PAGE_LEN = 10


def _capped_pages(last_page_cards, page_len=_ENDPOINT_PAGE_LEN):
    """Every page holds `page_len` cards except the last, which holds
    `last_page_cards`. Keyed by the offsets a cards-returned stride requests."""
    from src.scraper import _MAX_PAGES_PER_QUERY
    starts = [page_len * n for n in range(_MAX_PAGES_PER_QUERY)]
    return {
        start: _search_html([
            (start + n, "AI Engineer", "Berlin, Germany")
            for n in range(last_page_cards if start == starts[-1] else page_len)
        ])
        for start in starts
    }


def test_page_cap_hit_is_reported_when_the_last_page_was_full(monkeypatch, capsys):
    mock_get, calls = _paging_mock_get(_capped_pages(_ENDPOINT_PAGE_LEN))
    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda s: None)

    fetch_offers(["AI Engineer"], "Europe", "r86400")

    assert "Hit the page cap" in capsys.readouterr().out


def test_page_cap_hit_is_reported_for_any_endpoint_page_size(monkeypatch, capsys):
    # "Full" is measured against the pages this query actually saw, so the
    # report keeps working if the endpoint's page size moves again (25 -> 10
    # in 2026-09) instead of silently never firing.
    mock_get, calls = _paging_mock_get(_capped_pages(7, page_len=7))
    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda s: None)

    fetch_offers(["AI Engineer"], "Europe", "r86400")

    assert "Hit the page cap" in capsys.readouterr().out


def test_page_cap_hit_is_reported_when_the_last_full_page_overlaps_the_previous(monkeypatch, capsys):
    # LinkedIn's offset window shifts while a crawl runs, so a genuinely full
    # final page can repeat a couple of cards from the page before it. That
    # overlap must not mask the cap-hit report.
    from src.scraper import _MAX_PAGES_PER_QUERY
    pages = _capped_pages(_ENDPOINT_PAGE_LEN)
    last_start = _ENDPOINT_PAGE_LEN * (_MAX_PAGES_PER_QUERY - 1)
    prev_start = last_start - _ENDPOINT_PAGE_LEN
    pages[last_start] = _search_html(
        [(prev_start + n, "AI Engineer", "Berlin, Germany") for n in range(2)]
        + [(last_start + n, "AI Engineer", "Berlin, Germany")
           for n in range(_ENDPOINT_PAGE_LEN - 2)]
    )
    mock_get, calls = _paging_mock_get(pages)
    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda s: None)

    fetch_offers(["AI Engineer"], "Europe", "r86400")

    assert "Hit the page cap" in capsys.readouterr().out


def test_no_page_cap_report_when_the_last_page_was_under_full(monkeypatch, capsys):
    mock_get, calls = _paging_mock_get(_capped_pages(_ENDPOINT_PAGE_LEN - 4))
    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda s: None)

    fetch_offers(["AI Engineer"], "Europe", "r86400")

    assert "Hit the page cap" not in capsys.readouterr().out


def test_hard_error_ending_pagination_is_reported(monkeypatch, capsys):
    pages = {0: _search_html([(1, "AI Engineer", "Berlin, Germany")]), 1: 400}
    mock_get, calls = _paging_mock_get(pages)
    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda s: None)
    monkeypatch.setattr("src.scraper.random.uniform", lambda a, b: a)

    offers = fetch_offers(["AI Engineer"], "Europe", "r86400")

    out = capsys.readouterr().out
    assert "Hard error on page 1" in out
    assert "keeping 1 offers already fetched" in out
    assert len(offers) == 1


def test_pagination_propagates_a_403_instead_of_ending_results(monkeypatch):
    # A 403 means the guest endpoint is blocking this IP, which plausibly
    # compromises the whole remaining crawl, not just this one query - it
    # must reach run_tier_with_retry as a real error rather than being
    # silently absorbed as end-of-results after a truncated offer list.
    pages = {0: _search_html([(1, "AI Engineer", "Berlin, Germany")]), 1: 403}
    mock_get, calls = _paging_mock_get(pages)
    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda s: None)
    monkeypatch.setattr("src.scraper.random.uniform", lambda a, b: a)

    with pytest.raises(RuntimeError, match="403"):
        fetch_offers(["AI Engineer"], "Europe", "r86400")


def test_no_hard_error_report_when_pagination_ends_naturally(monkeypatch, capsys):
    pages = {0: _search_html([(1, "AI Engineer", "Berlin, Germany")]), 1: EMPTY_PAGE_HTML}
    mock_get, calls = _paging_mock_get(pages)
    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda s: None)

    fetch_offers(["AI Engineer"], "Europe", "r86400")

    assert "Hard error on page" not in capsys.readouterr().out


def test_no_page_cap_report_when_an_empty_page_ends_pagination(monkeypatch, capsys):
    pages = {0: _search_html([(1, "AI Engineer", "Berlin, Germany")]), 1: EMPTY_PAGE_HTML}
    mock_get, calls = _paging_mock_get(pages)
    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda s: None)

    fetch_offers(["AI Engineer"], "Europe", "r86400")

    assert "Hit the page cap" not in capsys.readouterr().out


def test_no_page_cap_report_when_a_repeated_page_ends_pagination(monkeypatch, capsys):
    page = _search_html([(1, "AI Engineer", "Berlin, Germany")])
    mock_get, calls = _paging_mock_get({0: page, 1: page})
    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda s: None)

    fetch_offers(["AI Engineer"], "Europe", "r86400")

    assert "Hit the page cap" not in capsys.readouterr().out


def test_no_page_cap_report_when_end_of_results_ends_pagination(monkeypatch, capsys):
    pages = {0: _search_html([(1, "AI Engineer", "Berlin, Germany")]), 1: 400}
    mock_get, calls = _paging_mock_get(pages)
    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda s: None)
    monkeypatch.setattr("src.scraper.random.uniform", lambda a, b: a)

    fetch_offers(["AI Engineer"], "Europe", "r86400")

    assert "Hit the page cap" not in capsys.readouterr().out


def _event_log_mock_get(pages, log):
    """Wraps _paging_mock_get so search requests land in a shared event log
    alongside sleeps, letting a test assert their relative order."""
    inner, calls = _paging_mock_get(pages)

    def mock_get(url, **kwargs):
        log.append("search" if "seeMoreJobPostings" in url else "description")
        return inner(url, **kwargs)

    return mock_get, calls


def test_search_pages_are_paced_but_the_first_request_is_not_delayed(monkeypatch):
    from src.tier_scope import TIER3_ALLOWED_COUNTRIES
    log = []
    # Every card is out of EU/EEA scope, so no description fetch (and no card
    # loop sleep) happens - the page sleep is the only pacing left.
    pages = {
        0: _search_html([(1, "AI Engineer", "London, United Kingdom")]),
        1: _search_html([(2, "AI Engineer", "Zurich, Switzerland")]),
        2: EMPTY_PAGE_HTML,
    }
    mock_get, calls = _event_log_mock_get(pages, log)
    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda s: log.append("sleep"))

    offers = fetch_offers(["AI Engineer"], "Europe", "r86400",
                          allowed_countries=TIER3_ALLOWED_COUNTRIES)

    assert offers == []
    assert "description" not in log
    assert log == ["search", "sleep", "search", "sleep", "search"]


def test_a_single_page_query_never_sleeps_before_its_only_request(monkeypatch):
    log = []
    mock_get, calls = _event_log_mock_get({0: EMPTY_PAGE_HTML}, log)
    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda s: log.append("sleep"))

    fetch_offers(["AI Engineer"], "Europe", "r86400")

    assert log == ["search"]


# --- per-tier configurable page cap ---------------------------------------


def test_resolve_max_pages_per_query_returns_a_valid_value():
    from src.scraper import resolve_max_pages_per_query
    assert resolve_max_pages_per_query(30) == 30


def test_resolve_max_pages_per_query_falls_back_to_default_when_absent():
    from src.scraper import resolve_max_pages_per_query, _MAX_PAGES_PER_QUERY
    assert resolve_max_pages_per_query(None) == _MAX_PAGES_PER_QUERY


@pytest.mark.parametrize("bad_value", [0, -1, "30", 3.5, True, False])
def test_resolve_max_pages_per_query_falls_back_to_default_when_invalid(bad_value):
    from src.scraper import resolve_max_pages_per_query, _MAX_PAGES_PER_QUERY
    assert resolve_max_pages_per_query(bad_value) == _MAX_PAGES_PER_QUERY


def test_pagination_honours_a_custom_max_pages_per_query(monkeypatch):
    pages = {
        start: _search_html([(start + 1, "AI Engineer", "Berlin, Germany")])
        for start in range(40)
    }
    mock_get, calls = _paging_mock_get(pages)
    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda s: None)

    fetch_offers(["AI Engineer"], "Europe", "r86400", max_pages_per_query=3)

    assert len(calls["search_starts"]) == 3


def test_pagination_ends_before_a_generous_cap_when_results_run_out(monkeypatch):
    # The early-stop conditions (empty page / all-duplicate page / end-of-
    # results) must still end a shallow query well short of a large cap - a
    # generous cap is only safe because a shallow search still costs nothing.
    pages = {
        0: _search_html([(1, "AI Engineer", "Berlin, Germany"), (2, "ML Engineer", "Paris, France")]),
        2: EMPTY_PAGE_HTML,
    }
    mock_get, calls = _paging_mock_get(pages)
    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda s: None)

    offers = fetch_offers(["AI Engineer"], "Europe", "r86400", max_pages_per_query=30)

    assert len(offers) == 2
    assert calls["search_starts"] == [0, 2]


def test_page_cap_hit_message_reports_the_custom_cap(monkeypatch, capsys):
    mock_get, calls = _paging_mock_get(_capped_pages(_ENDPOINT_PAGE_LEN))
    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda s: None)

    fetch_offers(["AI Engineer"], "Europe", "r86400", max_pages_per_query=3)

    assert "Hit the page cap (3 pages)" in capsys.readouterr().out


def test_per_query_page_count_is_logged(monkeypatch, capsys):
    pages = {
        0: _search_html([(1, "AI Engineer", "Berlin, Germany")]),
        1: EMPTY_PAGE_HTML,
    }
    mock_get, calls = _paging_mock_get(pages)
    monkeypatch.setattr("src.scraper.requests.get", mock_get)
    monkeypatch.setattr("src.scraper.time.sleep", lambda s: None)

    fetch_offers(["AI Engineer"], "Europe", "r86400")

    out = capsys.readouterr().out
    assert "AI Engineer/Europe/None: 2/8 pages, 1 offers kept." in out


def _page(n, start=0):
    """Search HTML holding `n` distinct cards whose job ids begin at `start`."""
    return _search_html([(start + i, "AI Engineer", "Berlin, Germany") for i in range(n)])


def _run_query(pages, cap=3):
    responses = iter(pages)

    def fake_page(role, location, time_range, work_mode, start):
        item = next(responses)
        if isinstance(item, Exception):
            raise item
        return type("R", (), {"text": item})()

    with patch("src.scraper._fetch_search_page", side_effect=fake_page), \
         patch("src.scraper._fetch_description", return_value=("desc", "ok")), \
         patch("src.scraper.time.sleep"), \
         patch("src.scraper.telemetry.add_query") as add_query:
        try:
            scraper._fetch_for_query("AI Engineer", "Italy", "r86400", "remote",
                                     max_pages_per_query=cap)
        except Exception:
            pass
    return add_query.call_args.kwargs


def test_stop_reason_cap_hit_when_the_last_allowed_page_is_full():
    rec = _run_query([_page(10, 0), _page(10, 10), _page(10, 20)], cap=3)
    assert rec["stop_reason"] == "cap_hit" and rec["pages_walked"] == 3 and rec["page_cap"] == 3


def test_stop_reason_exhausted_underfull_when_the_cap_page_is_short():
    rec = _run_query([_page(10, 0), _page(10, 10), _page(4, 20)], cap=3)
    assert rec["stop_reason"] == "exhausted_underfull"


def test_results_ending_exactly_at_the_cap_are_not_a_cap_hit():
    rec = _run_query([_page(10, 0), _page(10, 10), ""], cap=3)
    assert rec["stop_reason"] == "empty_page" and rec["cards_seen"] == 20


def test_stop_reason_duplicate_page():
    rec = _run_query([_page(10, 0), _page(10, 0)], cap=5)
    assert rec["stop_reason"] == "duplicate_page"


def test_stop_reason_end_of_results_after_page_zero():
    rec = _run_query([_page(10, 0), scraper._EndOfResults("400")], cap=5)
    assert rec["stop_reason"] == "end_of_results" and rec["offers_kept"] == 10


def test_stop_reason_error_is_recorded_and_the_error_still_propagates():
    with patch("src.scraper._fetch_search_page", side_effect=RuntimeError("403")), \
         patch("src.scraper.telemetry.add_query") as add_query:
        with pytest.raises(RuntimeError):
            scraper._fetch_for_query("AI Engineer", "Italy", "r86400", "remote", max_pages_per_query=3)
    assert add_query.call_args.kwargs["stop_reason"] == "error"


def test_search_429s_are_counted_but_503s_are_not():
    ok = type("R", (), {"status_code": 200, "text": ""})()
    limited = type("R", (), {"status_code": 429, "text": ""})()
    unavailable = type("R", (), {"status_code": 503, "text": ""})()
    with patch("src.scraper.requests.get", side_effect=[limited, unavailable, ok]), \
         patch("src.scraper.time.sleep"), \
         patch("src.scraper.telemetry.count") as count:
        scraper._fetch_search_page("r", "l", "t", None, 0)
    count.assert_called_once_with("search_rate_limits")


def test_description_429s_are_counted_but_503s_are_not():
    ok = type("R", (), {"status_code": 200, "text": "<p></p>"})()
    limited = type("R", (), {"status_code": 429, "text": ""})()
    unavailable = type("R", (), {"status_code": 503, "text": ""})()
    with patch("src.scraper.requests.get", side_effect=[limited, unavailable, ok]), \
         patch("src.scraper.time.sleep"), \
         patch("src.scraper.telemetry.count") as count:
        scraper._fetch_description("https://example.com/j", "T", "C")
    count.assert_called_once_with("description_rate_limits")


# --- cross-query description dedup ---------------------------------------

_OVERLAP_FIXTURE = {
    "Role A": [1, 2, 3],
    "Role B": [2, 4, 3, 5],
    "Role C": [5, 1, 6],
}


def _run_overlap_fixture(fresh_cache_per_query=False):
    """Run fetch_offers over overlapping queries; return (offers, fetched links,
    the add_query record kwargs per query in call order).

    fresh_cache_per_query=True hands every query its own empty cache, which is
    exactly the pre-change behaviour (each query fetches every card it sees)."""
    fetched = []

    def fake_page(role, location, time_range, work_mode, start):
        cards = _OVERLAP_FIXTURE[role] if start == 0 else []
        return type("R", (), {"text": _search_html(
            [(i, f"Job {i}", "Berlin, Germany") for i in cards])})()

    def fake_description(url, title, company):
        fetched.append(url)
        return f"description of {url}", "ok"

    real = scraper._fetch_for_query

    def per_query_cache(*args, **kwargs):
        kwargs["fetched_descriptions"] = {}
        return real(*args, **kwargs)

    target = per_query_cache if fresh_cache_per_query else real
    with patch("src.scraper._fetch_search_page", side_effect=fake_page), \
         patch("src.scraper._fetch_description", side_effect=fake_description), \
         patch("src.scraper._fetch_for_query", side_effect=target), \
         patch("src.scraper.time.sleep"), \
         patch("src.scraper.telemetry.add_query") as add_query:
        offers = fetch_offers(list(_OVERLAP_FIXTURE), "Europe", "r86400")
    return offers, fetched, [c.kwargs for c in add_query.call_args_list]


def test_a_link_surfaced_by_two_queries_is_fetched_once():
    _, fetched, _ = _run_overlap_fixture()

    assert len(fetched) == len(set(fetched)) == 6
    assert sorted(fetched) == sorted(
        f"https://www.linkedin.com/jobs/view/{i}" for i in range(1, 7))


def test_returned_offers_equal_the_pre_change_result():
    new_offers, new_fetched, _ = _run_overlap_fixture()
    old_offers, old_fetched, _ = _run_overlap_fixture(fresh_cache_per_query=True)

    assert [o.model_dump() for o in new_offers] == [o.model_dump() for o in old_offers]
    # Same fixture, so the reference really did pay for the duplicates.
    assert len(old_fetched) == 10 and len(new_fetched) == 6
    # First occurrence wins, ids keep the gaps the discarded duplicates leave.
    assert [(o.id, o.link.rsplit("/", 1)[1]) for o in new_offers] == [
        (0, "1"), (1, "2"), (2, "3"), (4, "4"), (6, "5"), (9, "6")]


def test_per_search_records_stay_consistent_with_cross_query_reuse():
    offers, fetched, records = _run_overlap_fixture()

    assert [(r["role"], r["cards_seen"], r["offers_kept"], r["cross_query_duplicates"])
            for r in records] == [
        ("Role A", 3, 3, 0),
        ("Role B", 4, 4, 2),
        ("Role C", 3, 3, 2),
    ]
    # Every kept offer is either a real fetch or a counted reuse, never both.
    assert sum(r["offers_kept"] - r["cross_query_duplicates"] for r in records) == len(fetched)
    assert all(r["stop_reason"] == "empty_page" for r in records)
    assert len(offers) == len(fetched)


def test_reused_duplicate_costs_no_pacing_sleep():
    sleeps = []
    responses = {"Role A": [1, 2], "Role B": [1, 2]}

    def fake_page(role, location, time_range, work_mode, start):
        cards = responses[role] if start == 0 else []
        return type("R", (), {"text": _search_html([(i, f"Job {i}", "Berlin, Germany") for i in cards])})()

    with patch("src.scraper._fetch_search_page", side_effect=fake_page), \
         patch("src.scraper._fetch_description", return_value=("d", "ok")), \
         patch("src.scraper.time.sleep", side_effect=sleeps.append), \
         patch("src.scraper.random.uniform", return_value=2.0), \
         patch("src.scraper.telemetry.add_query"):
        fetch_offers(["Role A", "Role B"], "Europe", "r86400")

    # Role A: 2 description sleeps + 1 page sleep (page 1). Role B: only its page sleep.
    assert len(sleeps) == 3 + 1


def test_duplicate_page_stop_ignores_other_queries_links():
    # Role B's first page is entirely made of links Role A already fetched. The
    # per-query duplicate-page stop must not treat that as a repeated page.
    pages = {"Role A": {0: [1, 2], 2: []}, "Role B": {0: [1, 2], 2: []}}

    def fake_page(role, location, time_range, work_mode, start):
        cards = pages[role][start]
        return type("R", (), {"text": _search_html([(i, f"Job {i}", "Berlin, Germany") for i in cards])})()

    with patch("src.scraper._fetch_search_page", side_effect=fake_page), \
         patch("src.scraper._fetch_description", return_value=("d", "ok")), \
         patch("src.scraper.time.sleep"), \
         patch("src.scraper.telemetry.add_query") as add_query:
        fetch_offers(["Role A", "Role B"], "Europe", "r86400")

    role_b = add_query.call_args_list[1].kwargs
    assert role_b["stop_reason"] == "empty_page" and role_b["pages_walked"] == 2
    assert role_b["offers_kept"] == 2 and role_b["cross_query_duplicates"] == 2
