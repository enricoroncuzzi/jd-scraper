import os
import pytest
import tailor as tailor_cli


def test_resolve_uri_basic(tmp_path):
    root = str(tmp_path)
    got = tailor_cli.resolve_uri("tailor:2026-07-02/tier1/scraped/x_63.md", root)
    assert got == os.path.join(root, "2026-07-02/tier1/scraped/x_63.md")


def test_resolve_uri_double_slash_scheme(tmp_path):
    root = str(tmp_path)
    got = tailor_cli.resolve_uri("tailor://2026-07-02/tier1/scraped/x_63.md", root)
    assert got == os.path.join(root, "2026-07-02/tier1/scraped/x_63.md")


def test_resolve_uri_percent_decodes(tmp_path):
    root = str(tmp_path)
    got = tailor_cli.resolve_uri("tailor:2026-07-02/tier1/scraped/a%20b_1.md", root)
    assert got.endswith("scraped/a b_1.md")


def test_resolve_uri_rejects_traversal(tmp_path):
    with pytest.raises(ValueError, match="outside"):
        tailor_cli.resolve_uri("tailor:../../etc/passwd", str(tmp_path))


def test_handle_uri_success_notifies_and_reveals(tmp_path, monkeypatch):
    calls = {"notify": [], "reveal": []}
    monkeypatch.setattr(tailor_cli, "run", lambda *a, **k: str(tmp_path / "out" / "acme"))
    monkeypatch.setattr(tailor_cli, "notify", lambda title, msg: calls["notify"].append((title, msg)))
    monkeypatch.setattr(tailor_cli, "reveal", lambda p: calls["reveal"].append(p))

    out = tailor_cli.handle_uri(
        "tailor:2026-07-02/tier1/scraped/acme_1.md",
        str(tmp_path), "master.md", "css.md", "key",
    )
    assert out.endswith("acme")
    assert calls["notify"] == [("CV tailored", "acme")]
    assert calls["reveal"] == [str(tmp_path / "out" / "acme")]


def test_handle_uri_failure_notifies_error(tmp_path, monkeypatch):
    calls = []
    def boom(*a, **k):
        raise ValueError("generation returned no result")
    monkeypatch.setattr(tailor_cli, "run", boom)
    monkeypatch.setattr(tailor_cli, "notify", lambda title, msg: calls.append((title, msg)))
    monkeypatch.setattr(tailor_cli, "reveal", lambda p: None)

    with pytest.raises(ValueError):
        tailor_cli.handle_uri(
            "tailor:2026-07-02/tier1/scraped/acme_1.md",
            str(tmp_path), "master.md", "css.md", "key",
        )
    assert calls and calls[0][0] == "Tailoring failed"
    assert "generation returned no result" in calls[0][1]
