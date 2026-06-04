import os
from datetime import date
from src.models import ScoredOffer
from src.obsidian import write_notes, write_digest


def _offer(id, title, company, score, location="EU", description="desc"):
    return ScoredOffer(
        id=id, title=title, company=company, location=location,
        link=f"https://li.com/{id}", description=description,
        score=score, comment="ok", summary="summary here"
    )


def _scraped_dir(tmp_path):
    today = date.today().strftime("%Y%m%d")
    return tmp_path / "jobs" / f"scraped_{today}"


def test_write_notes_creates_one_file_per_offer(tmp_path):
    offers = [_offer(0, "AI Engineer", "Acme", 9), _offer(1, "Java Dev", "Corp", 3)]
    write_notes(offers, str(tmp_path), threshold=8)
    files = list(_scraped_dir(tmp_path).glob("*.md"))
    assert len(files) == 2


def test_write_notes_high_score_tag(tmp_path):
    offers = [_offer(0, "AI Engineer", "Acme", 9)]
    write_notes(offers, str(tmp_path), threshold=8)
    files = list(_scraped_dir(tmp_path).glob("*.md"))
    content = files[0].read_text()
    assert "high-score" in content
    assert "score: 9" in content
    assert "AI Engineer" in content
    assert "Acme" in content


def test_write_notes_low_score_tag(tmp_path):
    offers = [_offer(0, "Java Dev", "Corp", 3)]
    write_notes(offers, str(tmp_path), threshold=8)
    files = list(_scraped_dir(tmp_path).glob("*.md"))
    assert "low-score" in files[0].read_text()


def test_write_notes_malformed_offer(tmp_path):
    offer = ScoredOffer(id=0, title="Unknown", company="N/A", location="N/A",
                        link="https://li.com/0", description="",
                        score=1, comment="Description unavailable — could not evaluate.",
                        summary="")
    write_notes([offer], str(tmp_path), threshold=8)
    files = list(_scraped_dir(tmp_path).glob("*.md"))
    content = files[0].read_text()
    assert "Description unavailable" in content
    assert "score: 1" in content


def test_write_notes_folder_is_date_stamped(tmp_path):
    offers = [_offer(0, "AI Engineer", "Acme", 9)]
    write_notes(offers, str(tmp_path), threshold=8)
    today = date.today().strftime("%Y%m%d")
    assert (tmp_path / "jobs" / f"scraped_{today}").exists()
    assert not (tmp_path / "jobs" / "scraped").exists()


def test_write_digest_no_offers(tmp_path):
    write_digest([], str(tmp_path), threshold=8)
    files = list((tmp_path / "jobs" / "digest").glob("*.md"))
    assert len(files) == 1
    assert "No new offers after dedup filter" in files[0].read_text()


def test_write_digest_has_high_and_low_sections(tmp_path):
    offers = [
        _offer(0, "AI Engineer", "Acme", 9),
        _offer(1, "Java Dev", "Corp", 4),
    ]
    write_digest(offers, str(tmp_path), threshold=8)
    content = list((tmp_path / "jobs" / "digest").glob("*.md"))[0].read_text()
    assert "High-Score" in content
    assert "Low-Score" in content
    assert "AI Engineer" in content
    assert "1 offers below threshold" in content


def test_write_digest_contains_wikilink_to_note(tmp_path):
    offers = [_offer(0, "AI Engineer", "Acme", 9)]
    write_digest(offers, str(tmp_path), threshold=8)
    content = list((tmp_path / "jobs" / "digest").glob("*.md"))[0].read_text()
    today = date.today().isoformat()
    expected_link = f"[[{today}_acme_ai_engineer]]"
    assert expected_link in content


def test_write_digest_only_high(tmp_path):
    offers = [_offer(0, "AI Engineer", "Acme", 9)]
    write_digest(offers, str(tmp_path), threshold=8)
    content = list((tmp_path / "jobs" / "digest").glob("*.md"))[0].read_text()
    assert "High-Score" in content
    assert "Low-Score" not in content
