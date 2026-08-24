import json
import os
from src.models import ScoredOffer
from src.autoapply.package import write_manifest, notify_package


def _offer(**overrides):
    base = dict(
        id=1, title="AI Engineer", company="Acme", link="https://li.com/1",
        score=9, comment="great", summary="LLM role",
    )
    base.update(overrides)
    return ScoredOffer(**base)


def test_write_manifest_writes_json_with_expected_fields(tmp_path):
    directory = str(tmp_path)
    path = write_manifest(directory, _offer(), "external_ats", dry_run=False)
    assert os.path.exists(path)
    manifest = json.loads(open(path).read())
    assert manifest["title"] == "AI Engineer"
    assert manifest["company"] == "Acme"
    assert manifest["link"] == "https://li.com/1"
    assert manifest["channel"] == "external_ats"
    assert manifest["dry_run"] is False
    assert manifest["artifacts"]["cv_pdf"].endswith("Roncuzzi_CV.pdf")
    assert manifest["artifacts"]["cover_letter_pdf"].endswith("Roncuzzi_CL.pdf")


def test_write_manifest_records_dry_run_flag(tmp_path):
    path = write_manifest(str(tmp_path), _offer(), "email_apply", dry_run=True)
    manifest = json.loads(open(path).read())
    assert manifest["dry_run"] is True


def test_notify_package_notifies_and_reveals(monkeypatch):
    calls = {"notify": [], "reveal": []}
    monkeypatch.setattr(
        "src.autoapply.package.notify",
        lambda title, msg: calls["notify"].append((title, msg)),
    )
    monkeypatch.setattr(
        "src.autoapply.package.reveal",
        lambda p: calls["reveal"].append(p),
    )
    notify_package(_offer(), "linkedin_easy_apply", "/tmp/acme")
    assert len(calls["notify"]) == 1
    title, message = calls["notify"][0]
    assert "AI Engineer" in message
    assert "Acme" in message
    assert "LinkedIn Easy Apply" in message
    assert calls["reveal"] == ["/tmp/acme"]


def test_write_manifest_writes_only_the_manifest_file(tmp_path):
    directory = str(tmp_path)
    before = set(os.listdir(directory))
    write_manifest(directory, _offer(), "external_ats", dry_run=False)
    after = set(os.listdir(directory))
    assert after - before == {"APPLICATION_PACKAGE.json"}


def test_notify_package_side_effects_are_limited_to_notify_and_reveal(monkeypatch):
    calls = {"notify": [], "reveal": []}
    monkeypatch.setattr(
        "src.autoapply.package.notify",
        lambda title, msg: calls["notify"].append((title, msg)),
    )
    monkeypatch.setattr(
        "src.autoapply.package.reveal",
        lambda p: calls["reveal"].append(p),
    )
    result = notify_package(_offer(), "linkedin_easy_apply", "/tmp/acme")
    assert result is None
    assert len(calls["notify"]) == 1
    assert calls["reveal"] == ["/tmp/acme"]
