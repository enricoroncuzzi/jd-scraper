"""The legacy src/writer.py is the oracle: the same jobs must produce the same bytes."""
import re
from datetime import date, datetime, timezone

import pytest

from src import writer
from src.delivery.jobs import DeliveryJob, to_scored_offer
from src.delivery.render import note_filename, render_digest, render_note, render_rejected

DAY = date(2026, 10, 14)
ALERT_TIME = datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc)


class FrozenDate(date):
    @classmethod
    def today(cls):
        return DAY


@pytest.fixture(autouse=True)
def _freeze_the_legacy_writer_clock(monkeypatch):
    monkeypatch.setattr(writer, "date", FrozenDate)


def _job(n, score, *, tier=1, verdict="confirmed", title=None, company=None, status="ok",
         description="We build things.\nSecond line.", reason="Fully remote across the EU.", **kw):
    values = dict(
        id=f"00000000-0000-0000-0000-{n:012d}", note_no=100 + n, title=title or f"AI Engineer {n}",
        company=company or f"Acme {n} S.r.l.", location_raw="Milan, Italy",
        canonical_url=f"https://www.linkedin.com/jobs/view/ai-{n}", description=description,
        description_status=status, work_mode="remote", remote_verdict=verdict, remote_reason=reason,
        score=score, comment="Good fit.", summary=f"Summary {n}.", tier=tier, note_day=None, alerted_at=None)
    values.update(kw)
    return DeliveryJob(**values)


def _jobs():
    return [
        _job(1, 9), _job(2, 8, verdict="unconfirmed", reason="Silent about the work mode."),
        _job(3, 5), _job(4, 10, title="ML Engineer: RAG/LLM (m/f/d)", company="Beta GmbH & Co. KG"),
        _job(5, 3, status="failed", description=""), _job(6, 7, verdict="unconfirmed"),
    ]


def _legacy_files(tmp_path, jobs, verification_enabled, deferred=0, tier=1):
    offers = [to_scored_offer(j) for j in jobs]
    writer.write_notes(offers, str(tmp_path), 8, tier)
    writer.write_digest(offers, str(tmp_path), 8, tier, verification_enabled, deferred_count=deferred)
    return tmp_path / DAY.isoformat() / f"tier{tier}"


def _strip_added_fields(text):
    return re.sub(r"^(source|also_seen_on): .*\n", "", text, flags=re.MULTILINE)


@pytest.mark.parametrize("verification_enabled", [True, False])
@pytest.mark.parametrize("deferred", [0, 4])
def test_the_digest_is_byte_identical_to_the_legacy_writer(tmp_path, verification_enabled, deferred):
    jobs = _jobs()
    legacy = (_legacy_files(tmp_path, jobs, verification_enabled, deferred) / "digest.md").read_text()
    new = render_digest(jobs, DAY, 8, 1, verification_enabled, deferred)
    assert new == legacy


def test_an_empty_digest_matches_in_both_variants(tmp_path):
    assert render_digest([], DAY, 8, 1, True, 0) == writer._format_digest([], DAY.isoformat(), 8, 1, 20, True, 0)
    assert render_digest([], DAY, 8, 1, True, 7) == writer._format_digest([], DAY.isoformat(), 8, 1, 20, True, 7)


def test_notes_have_the_same_names_and_bytes_apart_from_the_two_added_fields(tmp_path):
    jobs = _jobs()
    folder = _legacy_files(tmp_path, jobs, True) / "scraped"
    legacy = {p.name: p.read_text() for p in folder.glob("*.md")}
    assert len(legacy) == len(jobs)
    for job in jobs:
        name = note_filename(job)
        assert name in legacy
        new = render_note(job, 8, DAY)
        assert "source: linkedin\nalso_seen_on: []\ntags: [job, scraped," in new
        assert _strip_added_fields(new) == legacy[name]


def test_the_added_fields_list_other_sources(tmp_path):
    job = _job(1, 9, source="freehire", also_seen_on=["linkedin", "himalayas"])
    assert "source: freehire\nalso_seen_on: [linkedin, himalayas]\n" in render_note(job, 8, DAY)


def test_rejected_md_is_byte_identical_to_the_legacy_writer(tmp_path):
    rejected = [_job(7, 1, verdict="rejected", reason="Needs 3 office days."),
                _job(8, 1, verdict="rejected", reason="Italy-only residents.")]
    writer.write_rejected([to_scored_offer(j) for j in rejected], str(tmp_path), 1, True)
    legacy = (tmp_path / DAY.isoformat() / "tier1" / "rejected.md").read_text()
    assert render_rejected(rejected, DAY, 1) == legacy


def test_an_empty_rejected_md_matches_too(tmp_path):
    writer.write_rejected([], str(tmp_path), 3, True)
    assert render_rejected([], DAY, 3) == (tmp_path / DAY.isoformat() / "tier3" / "rejected.md").read_text()


def test_an_entry_for_a_note_written_by_an_earlier_alert_points_there_and_says_so():
    earlier = date(2026, 10, 13)
    jobs = [_job(1, 9, note_day=earlier, alerted_at=ALERT_TIME), _job(2, 9)]
    lines = render_digest(jobs, DAY, 8, 1, True).split("\n")
    alerted = next(l for l in lines if "AI Engineer 1" in l and l.startswith("- **"))
    normal = next(l for l in lines if "AI Engineer 2" in l and l.startswith("- **"))
    file = note_filename(jobs[0])
    assert f"[note](../../2026-10-13/tier1/scraped/{file})" in alerted
    assert f"tailor:2026-10-13/tier1/scraped/{file}" in alerted
    assert f"jdlabel:golden/2026-10-13/tier1/scraped/{file}" in alerted
    assert alerted.endswith(" · sent earlier")
    assert "sent earlier" not in normal and f"[note](scraped/{note_filename(jobs[1])})" in normal


def test_an_alert_earlier_the_same_day_is_marked_but_keeps_the_normal_links():
    jobs = [_job(1, 9, note_day=DAY, alerted_at=ALERT_TIME)]
    entry = next(l for l in render_digest(jobs, DAY, 8, 1, True).split("\n") if l.startswith("- **"))
    assert entry.endswith(" · sent earlier") and "[note](scraped/" in entry and "../../" not in entry
