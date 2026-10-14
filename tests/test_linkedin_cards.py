from datetime import datetime, timezone

from src.linkedin.cards import parse_cards

PAGE = """
<ul>
  <li>
    <div class="base-card base-search-card" data-entity-urn="urn:li:jobPosting:3912345678">
      <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/ai-engineer-at-acme-3912345678?trk=x&amp;position=1">
        <span class="sr-only">AI Engineer</span>
      </a>
      <h3 class="base-search-card__title">  AI Engineer </h3>
      <h4 class="base-search-card__subtitle"><a href="#">Acme S.r.l.</a></h4>
      <span class="job-search-card__location">Milan, Lombardy, Italy</span>
      <time class="job-search-card__listdate" datetime="2026-10-13">1 day ago</time>
    </div>
  </li>
  <li>
    <div class="base-card base-search-card" data-entity-urn="urn:li:jobPosting:3900000001">
      <a class="base-card__full-link" href="https://it.linkedin.com/jobs/view/3900000001"></a>
      <h3 class="base-search-card__title">ML Engineer</h3>
    </div>
  </li>
  <li><div>An ad block without a title</div></li>
  <li>
    <div class="base-card base-search-card" data-entity-urn="urn:li:jobPosting:3900000002">
      <a class="base-card__full-link" href="https://www.linkedin.com/comm/jobs/view?refId=abc"></a>
      <h3 class="base-search-card__title">Data Scientist</h3>
      <time datetime="not-a-date">today</time>
    </div>
  </li>
</ul>
"""


def test_cards_carry_id_clean_url_text_and_posted_date():
    first = parse_cards(PAGE)[0]
    assert first.job_id == "3912345678"
    assert first.url == "https://www.linkedin.com/jobs/view/ai-engineer-at-acme-3912345678"
    assert (first.title, first.company, first.location) == ("AI Engineer", "Acme S.r.l.", "Milan, Lombardy, Italy")
    assert first.posted_at == datetime(2026, 10, 13, tzinfo=timezone.utc)


def test_missing_company_location_and_date_use_the_old_defaults():
    second = parse_cards(PAGE)[1]
    assert second.job_id == "3900000001"
    assert (second.company, second.location, second.posted_at) == ("N/A", "N/A", None)


def test_blocks_without_a_title_are_skipped_and_the_urn_is_the_id_fallback():
    cards = parse_cards(PAGE)
    assert [c.job_id for c in cards] == ["3912345678", "3900000001", "3900000002"]
    assert cards[2].posted_at is None  # a malformed date never raises


def test_an_empty_page_has_no_cards():
    assert parse_cards("<html><body></body></html>") == []
