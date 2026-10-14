"""golden_check against a day folder written by the legacy writer (the oracle)."""
from datetime import date

import pytest

from src import writer
from src.delivery.golden_check import check_day, note_to_job
from src.delivery.jobs import to_scored_offer
from tests.test_delivery_golden import DAY, _job


@pytest.fixture
def legacy_day(tmp_path, monkeypatch):
    class Frozen(date):
        @classmethod
        def today(cls):
            return DAY
    monkeypatch.setattr(writer, "date", Frozen)
    for tier, verification in ((1, True), (2, False)):
        jobs = [_job(n + tier * 10, score, tier=tier, verdict=v, status=s, description=d)
                for n, (score, v, s, d) in enumerate([
                    (9, "confirmed", "ok", "Build things.\nSecond line."), (8, "unconfirmed", "ok", "Late remote text."),
                    (5, "confirmed", "partial", "Short."), (10, "confirmed", "ok", "Hello & goodbye <b>x</b>"),
                    (7, "confirmed", "ok", "Hash # in text\n\nand blank lines")])]
        offers = [to_scored_offer(j) for j in jobs]
        writer.write_notes(offers, str(tmp_path), 8, tier)
        writer.write_digest(offers, str(tmp_path), 8, tier, verification, deferred_count=3 if tier == 1 else 0)
    return tmp_path / DAY.isoformat()


def test_a_day_written_by_the_old_pipeline_checks_out_identical(legacy_day):
    assert check_day(str(legacy_day)) == []


def test_a_note_in_another_format_is_reported_with_its_file_and_line(legacy_day):
    # The check finds drift in the template, not edits to the data: the data is read from the note itself.
    note = next((legacy_day / "tier1" / "scraped").glob("*.md"))
    note.write_text(note.read_text().replace("**Score:**", "**Rating:**"))
    problems = check_day(str(legacy_day))
    assert len(problems) >= 1 and note.name in problems[0] and "differs near line" in problems[0]


def test_a_changed_digest_is_reported(legacy_day):
    digest = legacy_day / "tier2" / "digest.md"
    digest.write_text(digest.read_text().replace("Summary", "Sumary"))
    assert any("tier2/digest.md: lines differ" in p for p in check_day(str(legacy_day)))


def test_a_note_is_rebuilt_into_the_same_job(legacy_day):
    original = _job(10, 9, tier=1, description="Build things.\nSecond line.")
    text = next(p for p in (legacy_day / "tier1" / "scraped").glob("*_110.md")).read_text()
    rebuilt = note_to_job(text, "x_110.md")
    for field in ("title", "company", "location_raw", "description", "score", "comment", "summary",
                  "remote_verdict", "remote_reason", "work_mode", "tier", "note_no"):
        assert getattr(rebuilt, field) == getattr(original, field), field
