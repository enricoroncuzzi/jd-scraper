import os
from datetime import date
from src.models import ScoredOffer
from src.writer import write_notes, write_digest


def _offer(id, title, company, score, location="EU", description="desc", work_mode="remote"):
    return ScoredOffer(
        id=id, title=title, company=company, location=location,
        link=f"https://li.com/{id}", description=description,
        score=score, comment="ok", summary="summary here", work_mode=work_mode
    )


def _scraped_dir(tmp_path, tier):
    today = date.today().isoformat()
    return tmp_path / today / f"tier{tier}" / "scraped"


def _digest_dir(tmp_path, tier):
    today = date.today().isoformat()
    return tmp_path / today / f"tier{tier}"


def test_write_notes_creates_one_file_per_offer(tmp_path):
    offers = [_offer(0, "AI Engineer", "Acme", 9), _offer(1, "Java Dev", "Corp", 3)]
    write_notes(offers, str(tmp_path), threshold=8, tier=1)
    files = list(_scraped_dir(tmp_path, 1).glob("*.md"))
    assert len(files) == 2


def test_write_notes_high_score_tag(tmp_path):
    offers = [_offer(0, "AI Engineer", "Acme", 9)]
    write_notes(offers, str(tmp_path), threshold=8, tier=1)
    files = list(_scraped_dir(tmp_path, 1).glob("*.md"))
    content = files[0].read_text()
    assert "high-score" in content
    assert "score: 9" in content
    assert "tier: 1" in content
    assert "work_mode: remote" in content
    assert "AI Engineer" in content
    assert "Acme" in content


def test_write_notes_low_score_tag(tmp_path):
    offers = [_offer(0, "Java Dev", "Corp", 3)]
    write_notes(offers, str(tmp_path), threshold=8, tier=1)
    files = list(_scraped_dir(tmp_path, 1).glob("*.md"))
    assert "low-score" in files[0].read_text()


def test_write_notes_malformed_offer(tmp_path):
    offer = ScoredOffer(id=0, title="Unknown", company="N/A", location="N/A",
                        link="https://li.com/0", description="",
                        score=1, comment="Description unavailable — could not evaluate.",
                        summary="")
    write_notes([offer], str(tmp_path), threshold=8, tier=2)
    files = list(_scraped_dir(tmp_path, 2).glob("*.md"))
    content = files[0].read_text()
    assert "Description unavailable" in content
    assert "score: 1" in content
    assert "tier: 2" in content


def test_write_notes_folder_is_date_stamped(tmp_path):
    offers = [_offer(0, "AI Engineer", "Acme", 9)]
    write_notes(offers, str(tmp_path), threshold=8, tier=1)
    today = date.today().isoformat()
    assert (tmp_path / today / "tier1" / "scraped").exists()
    assert not (tmp_path / "jobs").exists()


def test_write_digest_no_offers(tmp_path):
    write_digest([], str(tmp_path), threshold=8, tier=1)
    tier_dir = _digest_dir(tmp_path, 1)
    assert "No new offers after dedup filter" in (tier_dir / "digest_remote.md").read_text()
    assert "No new offers after dedup filter" in (tier_dir / "digest_hybrid.md").read_text()


def test_write_digest_has_high_and_low_sections(tmp_path):
    offers = [
        _offer(0, "AI Engineer", "Acme", 9),
        _offer(1, "Java Dev", "Corp", 4),
    ]
    write_digest(offers, str(tmp_path), threshold=8, tier=1)
    content = (_digest_dir(tmp_path, 1) / "digest_remote.md").read_text()
    assert "High-Score" in content
    assert "Low-Score" in content
    assert "AI Engineer" in content
    assert "1 offers below threshold" in content


def test_write_digest_contains_relative_link_to_note(tmp_path):
    offers = [_offer(0, "AI Engineer", "Acme", 9)]
    write_digest(offers, str(tmp_path), threshold=8, tier=1)
    content = (_digest_dir(tmp_path, 1) / "digest_remote.md").read_text()
    assert "[note](scraped/acme_ai_engineer.md)" in content


def test_write_digest_only_high(tmp_path):
    offers = [_offer(0, "AI Engineer", "Acme", 9)]
    write_digest(offers, str(tmp_path), threshold=8, tier=1)
    content = (_digest_dir(tmp_path, 1) / "digest_remote.md").read_text()
    assert "High-Score" in content
    assert "Low-Score" not in content


def _scored(score, work_mode, title="Engineer", company="Acme"):
    return ScoredOffer(
        id=0, title=title, company=company, location="Remote", link="https://li.com/1",
        description="d", work_mode=work_mode, score=score, comment="c", summary="s"
    )


def test_write_digest_creates_nested_tier_and_mode_files(tmp_path):
    offers = [_scored(9, "remote"), _scored(7, "hybrid")]
    write_digest(offers, str(tmp_path), threshold=8, tier=2)
    tier_dir = _digest_dir(tmp_path, 2)
    assert (tier_dir / "digest_remote.md").exists()
    assert (tier_dir / "digest_hybrid.md").exists()


def test_write_digest_splits_offers_by_work_mode(tmp_path):
    offers = [
        _scored(9, "remote", title="Remote Role", company="RemoteCo"),
        _scored(9, "hybrid", title="Hybrid Role", company="HybridCo"),
    ]
    write_digest(offers, str(tmp_path), threshold=8, tier=1)
    tier_dir = _digest_dir(tmp_path, 1)
    remote_text = (tier_dir / "digest_remote.md").read_text()
    hybrid_text = (tier_dir / "digest_hybrid.md").read_text()
    assert "Remote Role" in remote_text
    assert "Hybrid Role" not in remote_text
    assert "Hybrid Role" in hybrid_text
    assert "Remote Role" not in hybrid_text


def test_write_digest_caps_high_score_section_at_offer_cap(tmp_path):
    offers = [_scored(9, "remote", title=f"Role {i}", company=f"Co{i}") for i in range(25)]
    write_digest(offers, str(tmp_path), threshold=8, tier=1, offer_cap=20)
    remote_text = (_digest_dir(tmp_path, 1) / "digest_remote.md").read_text()
    shown = sum(1 for i in range(25) if f"Role {i} —" in remote_text)
    assert shown == 20
