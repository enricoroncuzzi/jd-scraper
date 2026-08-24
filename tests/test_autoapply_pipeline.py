from unittest.mock import MagicMock
from src.models import ScoredOffer
from src.autoapply.pipeline import run_autoapply


def _offer(id, score, link=None, title="AI Engineer", company="Acme"):
    return ScoredOffer(
        id=id, title=title, company=company, link=link or f"https://li.com/{id}",
        score=score, comment="c", summary="s",
    )


def _patch_common(monkeypatch, tailor_dir="/out/acme", already_packaged=False, count_today=0):
    calls = {"classify": [], "tailor_run": [], "write_manifest": [], "notify": [],
             "save_channel": [], "save_application": []}

    monkeypatch.setattr(
        "src.autoapply.pipeline.classify_channel",
        lambda link: calls["classify"].append(link) or "external_ats",
    )
    monkeypatch.setattr(
        "src.autoapply.pipeline.storage.count_applications_packaged_today",
        lambda db_url: count_today,
    )
    monkeypatch.setattr(
        "src.autoapply.pipeline.storage.is_application_packaged",
        lambda db_url, link: already_packaged,
    )
    monkeypatch.setattr(
        "src.autoapply.pipeline.storage.save_application_channel",
        lambda db_url, link, channel: calls["save_channel"].append((link, channel)),
    )
    monkeypatch.setattr(
        "src.autoapply.pipeline.storage.save_application",
        lambda db_url, link, title, company, channel, dry_run: calls["save_application"].append(
            (link, channel, dry_run)
        ),
    )
    monkeypatch.setattr(
        "src.autoapply.pipeline.tailor_cli.run",
        lambda note_path, out_root, master, css, key: calls["tailor_run"].append(note_path) or tailor_dir,
    )
    monkeypatch.setattr(
        "src.autoapply.pipeline.write_manifest",
        lambda directory, offer, channel, dry_run: calls["write_manifest"].append(
            (directory, offer.id, channel, dry_run)
        ),
    )
    monkeypatch.setattr(
        "src.autoapply.pipeline.notify_package",
        lambda offer, channel, directory: calls["notify"].append((offer.id, channel, directory)),
    )
    return calls


def test_run_autoapply_skips_offers_below_threshold(monkeypatch):
    calls = _patch_common(monkeypatch)
    offers = [_offer(1, score=5)]
    results = run_autoapply(
        offers, threshold=8, output_path="/out", tier=1, db_url=None,
        cv_master_path="m", css_path="c", groq_api_key="k",
        daily_cap=5, dry_run=False,
    )
    assert results == []
    assert calls["classify"] == []


def test_run_autoapply_tailors_and_notifies_above_threshold_offer(monkeypatch):
    calls = _patch_common(monkeypatch)
    offers = [_offer(1, score=9)]
    results = run_autoapply(
        offers, threshold=8, output_path="/out", tier=1, db_url="postgresql://test",
        cv_master_path="m", css_path="c", groq_api_key="k",
        daily_cap=5, dry_run=False,
    )
    assert len(results) == 1
    assert calls["classify"] == ["https://li.com/1"]
    assert len(calls["tailor_run"]) == 1
    assert calls["notify"] == [(1, "external_ats", "/out/acme")]
    assert calls["save_application"] == [("https://li.com/1", "external_ats", False)]


def test_run_autoapply_dry_run_skips_notify_and_tracking(monkeypatch):
    calls = _patch_common(monkeypatch)
    offers = [_offer(1, score=9)]
    results = run_autoapply(
        offers, threshold=8, output_path="/out", tier=1, db_url="postgresql://test",
        cv_master_path="m", css_path="c", groq_api_key="k",
        daily_cap=5, dry_run=True,
    )
    assert len(results) == 1
    assert results[0]["dry_run"] is True
    assert calls["notify"] == []
    assert calls["save_application"] == []
    # dry-run still runs the full pipeline: classify + tailor + package
    assert calls["classify"] == ["https://li.com/1"]
    assert len(calls["tailor_run"]) == 1
    assert len(calls["write_manifest"]) == 1


def test_run_autoapply_respects_already_packaged_dedup(monkeypatch):
    calls = _patch_common(monkeypatch, already_packaged=True)
    offers = [_offer(1, score=9)]
    results = run_autoapply(
        offers, threshold=8, output_path="/out", tier=1, db_url="postgresql://test",
        cv_master_path="m", css_path="c", groq_api_key="k",
        daily_cap=5, dry_run=False,
    )
    assert results == []
    assert calls["tailor_run"] == []
    assert calls["notify"] == []


def test_run_autoapply_stops_at_daily_cap(monkeypatch):
    calls = _patch_common(monkeypatch, count_today=0)
    offers = [_offer(1, score=9), _offer(2, score=9), _offer(3, score=9)]
    results = run_autoapply(
        offers, threshold=8, output_path="/out", tier=1, db_url="postgresql://test",
        cv_master_path="m", css_path="c", groq_api_key="k",
        daily_cap=2, dry_run=False,
    )
    assert len(results) == 2
    assert len(calls["tailor_run"]) == 2


def test_run_autoapply_cap_accounts_for_already_packaged_today(monkeypatch):
    calls = _patch_common(monkeypatch, count_today=2)
    offers = [_offer(1, score=9), _offer(2, score=9)]
    results = run_autoapply(
        offers, threshold=8, output_path="/out", tier=1, db_url="postgresql://test",
        cv_master_path="m", css_path="c", groq_api_key="k",
        daily_cap=2, dry_run=False,
    )
    assert results == []
    assert calls["tailor_run"] == []


def test_run_autoapply_continues_past_tailoring_failure(monkeypatch):
    calls = _patch_common(monkeypatch)

    def boom(*a, **k):
        raise ValueError("HALTED: some violation")

    monkeypatch.setattr("src.autoapply.pipeline.tailor_cli.run", boom)
    offers = [_offer(1, score=9)]
    results = run_autoapply(
        offers, threshold=8, output_path="/out", tier=1, db_url="postgresql://test",
        cv_master_path="m", css_path="c", groq_api_key="k",
        daily_cap=5, dry_run=False,
    )
    assert results == []
    assert calls["notify"] == []


def test_run_autoapply_no_candidates_short_circuits_without_db_calls(monkeypatch):
    called = {"count": False}
    monkeypatch.setattr(
        "src.autoapply.pipeline.storage.count_applications_packaged_today",
        lambda db_url: called.__setitem__("count", True) or 0,
    )
    results = run_autoapply(
        [], threshold=8, output_path="/out", tier=1, db_url=None,
        cv_master_path="m", css_path="c", groq_api_key="k",
        daily_cap=5, dry_run=False,
    )
    assert results == []
    assert called["count"] is False
