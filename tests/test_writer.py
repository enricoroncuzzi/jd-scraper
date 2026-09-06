import os
from datetime import date
from src.models import ScoredOffer
from src.writer import write_notes, write_digest, write_rejected


def _offer(id, title, company, score, location="EU", description="desc", work_mode="remote"):
    return ScoredOffer(
        id=id, title=title, company=company, location=location,
        link=f"https://li.com/{id}", description=description,
        score=score, comment="ok", summary="summary here", work_mode=work_mode
    )


def _offer_with_status(id, title, company, score, status):
    return ScoredOffer(
        id=id, title=title, company=company, location="EU",
        link=f"https://li.com/{id}", description="desc",
        description_status=status,
        score=score, comment="ok", summary="summary here", work_mode="remote"
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
    write_digest([], str(tmp_path), threshold=8, tier=1, verification_enabled=False)
    tier_dir = _digest_dir(tmp_path, 1)
    assert "No new offers after dedup filter" in (tier_dir / "digest.md").read_text()


def test_write_digest_has_high_and_low_sections(tmp_path):
    offers = [
        _offer(0, "AI Engineer", "Acme", 9),
        _offer(1, "Java Dev", "Corp", 4),
    ]
    write_digest(offers, str(tmp_path), threshold=8, tier=1, verification_enabled=False)
    content = (_digest_dir(tmp_path, 1) / "digest.md").read_text()
    assert "High-Score" in content
    assert "Low-Score" in content
    assert "AI Engineer" in content
    assert "1 offers below threshold" in content


def test_write_digest_contains_relative_link_to_note(tmp_path):
    offers = [_offer(0, "AI Engineer", "Acme", 9)]
    write_digest(offers, str(tmp_path), threshold=8, tier=1, verification_enabled=False)
    content = (_digest_dir(tmp_path, 1) / "digest.md").read_text()
    assert "[note](scraped/acme_ai_engineer_0.md)" in content


def test_write_digest_only_high(tmp_path):
    offers = [_offer(0, "AI Engineer", "Acme", 9)]
    write_digest(offers, str(tmp_path), threshold=8, tier=1, verification_enabled=False)
    content = (_digest_dir(tmp_path, 1) / "digest.md").read_text()
    assert "High-Score" in content
    assert "Low-Score" not in content


def _scored(score, work_mode, title="Engineer", company="Acme"):
    return ScoredOffer(
        id=0, title=title, company=company, location="Remote", link="https://li.com/1",
        description="d", work_mode=work_mode, score=score, comment="c", summary="s"
    )


def test_write_digest_caps_high_score_section_at_offer_cap(tmp_path):
    offers = [_scored(9, "remote", title=f"Role {i}", company=f"Co{i}") for i in range(25)]
    write_digest(offers, str(tmp_path), threshold=8, tier=1, verification_enabled=False, offer_cap=20)
    text = (_digest_dir(tmp_path, 1) / "digest.md").read_text()
    shown = sum(1 for i in range(25) if f"Role {i} -" in text)
    assert shown == 20


def test_write_notes_no_warning_when_description_ok(tmp_path):
    offers = [_offer_with_status(0, "AI Engineer", "Acme", 9, "ok")]
    write_notes(offers, str(tmp_path), threshold=8, tier=1)
    files = list(_scraped_dir(tmp_path, 1).glob("*.md"))
    content = files[0].read_text()
    assert "⚠ description" not in content


def test_write_notes_warning_when_description_partial(tmp_path):
    offers = [_offer_with_status(0, "AI Engineer", "Acme", 9, "partial")]
    write_notes(offers, str(tmp_path), threshold=8, tier=1)
    files = list(_scraped_dir(tmp_path, 1).glob("*.md"))
    assert "⚠ description: partial" in files[0].read_text()


def test_write_notes_warning_when_description_failed(tmp_path):
    offers = [_offer_with_status(0, "AI Engineer", "Acme", 9, "failed")]
    write_notes(offers, str(tmp_path), threshold=8, tier=1)
    files = list(_scraped_dir(tmp_path, 1).glob("*.md"))
    assert "⚠ description: failed" in files[0].read_text()


def test_write_digest_contains_tailor_link(tmp_path):
    from datetime import date
    offers = [_offer(0, "AI Engineer", "Acme", 9)]
    write_digest(offers, str(tmp_path), threshold=8, tier=1, verification_enabled=False)
    content = (_digest_dir(tmp_path, 1) / "digest.md").read_text()
    today = date.today().isoformat()
    assert f"[🎯 tailor](tailor:{today}/tier1/scraped/acme_ai_engineer_0.md)" in content


def test_write_digest_low_score_has_no_tailor_link(tmp_path):
    offers = [_offer(0, "Java Dev", "Corp", 3)]
    write_digest(offers, str(tmp_path), threshold=8, tier=1, verification_enabled=False)
    content = (_digest_dir(tmp_path, 1) / "digest.md").read_text()
    assert "tailor:" not in content


def _scored_v(score, verdict, title="Role", company="Co", reason=""):
    return ScoredOffer(
        id=1, title=title, company=company, link="https://x/1", score=score,
        location="Berlin, Germany", summary="A role.",
        remote_verdict=verdict, remote_reason=reason,
    )


def test_digest_is_a_single_file(tmp_path):
    write_digest([_scored_v(9, "confirmed")], str(tmp_path), 8, tier=1,
                 verification_enabled=True)
    tier_dir = tmp_path / date.today().isoformat() / "tier1"
    assert (tier_dir / "digest.md").exists()
    assert not (tier_dir / "digest_remote.md").exists()
    assert not (tier_dir / "digest_hybrid.md").exists()


def test_digest_separates_confirmed_from_unconfirmed(tmp_path):
    offers = [
        _scored_v(9, "confirmed", title="Confirmed Role"),
        _scored_v(9, "unconfirmed", title="Unsure Role", reason="Says remote, no country."),
    ]
    write_digest(offers, str(tmp_path), 8, tier=3, verification_enabled=True)
    text = (tmp_path / date.today().isoformat() / "tier3" / "digest.md").read_text()

    assert "Confirmed full-remote" in text
    assert "Remote not confirmed" in text
    assert text.index("Confirmed Role") < text.index("Remote not confirmed")
    assert "Says remote, no country." in text


def test_digest_has_no_verdict_sections_when_verification_is_off(tmp_path):
    write_digest([_scored_v(9, "not_checked", title="Any Role")], str(tmp_path), 8,
                 tier=2, verification_enabled=False)
    text = (tmp_path / date.today().isoformat() / "tier2" / "digest.md").read_text()

    assert "Confirmed full-remote" not in text
    assert "Remote not confirmed" not in text
    assert "Any Role" in text


def test_rejected_file_lists_rejections_with_reasons(tmp_path):
    rejected = [_scored_v(1, "rejected", title="Onsite Role", reason="Needs two days on site.")]
    write_rejected(rejected, str(tmp_path), tier=3, verification_enabled=True)
    text = (tmp_path / date.today().isoformat() / "tier3" / "rejected.md").read_text()

    assert "Onsite Role" in text
    assert "Needs two days on site." in text


def test_rejected_file_is_written_even_when_empty(tmp_path):
    write_rejected([], str(tmp_path), tier=3, verification_enabled=True)
    path = tmp_path / date.today().isoformat() / "tier3" / "rejected.md"
    assert path.exists()
    assert "None" in path.read_text()


def test_rejected_file_is_not_written_for_an_unverified_tier(tmp_path):
    write_rejected([], str(tmp_path), tier=2, verification_enabled=False)
    assert not (tmp_path / date.today().isoformat() / "tier2" / "rejected.md").exists()


def test_note_frontmatter_carries_the_verdict(tmp_path):
    offer = _scored_v(9, "confirmed", reason="Remote anywhere in the EU.")
    write_notes([offer], str(tmp_path), 8, tier=1)
    scraped = tmp_path / date.today().isoformat() / "tier1" / "scraped"
    text = next(scraped.iterdir()).read_text()

    assert "remote_verdict: confirmed" in text
    assert "remote_reason: Remote anywhere in the EU." in text
