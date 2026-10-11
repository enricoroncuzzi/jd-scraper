"""Tests for scripts/jd-sync.sh: a 30-day local window over an rsync of day folders."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "jd-sync.sh"


def _date_stub(directory, today):
    bin_dir = directory / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "date"
    stub.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "+%Y-%m-%d" ]; then\n'
        f"  printf '%s\\n' '{today}'\n"
        "  exit 0\n"
        "fi\n"
        'echo "unexpected date args: $*" >&2\n'
        "exit 1\n"
    )
    stub.chmod(0o755)
    return bin_dir


def _run(tmp_path, *, today, remote, local, days=None, dry_run=False, env_overrides=None):
    """Run jd-sync.sh under /bin/bash with a fixed calendar day and no network."""
    bin_dir = _date_stub(tmp_path / "stub-home", today)
    env = os.environ.copy()
    # macOS default userland (/usr/bin/rsync is openrsync). The date stub wins.
    env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
    env["JD_SYNC_REMOTE"] = str(remote)
    env["JD_SYNC_LOCAL"] = str(local)
    env.pop("JD_SYNC_DAYS", None)
    if days is not None:
        env["JD_SYNC_DAYS"] = str(days)
    if env_overrides:
        for key, value in env_overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
    cmd = ["/bin/bash", str(SCRIPT)]
    if dry_run:
        cmd.append("--dry-run")
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def _sync_lines(stdout):
    prefix = "would sync: "
    return [line[len(prefix):] for line in stdout.splitlines() if line.startswith(prefix)]


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_script_is_executable():
    assert os.access(SCRIPT, os.X_OK)


def test_window_crosses_month_boundary(tmp_path):
    remote = tmp_path / "remote"
    local = tmp_path / "local"
    remote.mkdir()
    local.mkdir()

    result = _run(tmp_path, today="2026-03-01", remote=remote, local=local, dry_run=True)

    assert result.returncode == 0, result.stderr
    synced = _sync_lines(result.stdout)
    # 30 days ending 2026-03-01 includes 2026-01-31 and excludes 2026-02-29.
    assert synced[0] == "2026-01-31"
    assert synced[-1] == "2026-03-01"
    assert len(synced) == 30
    assert "2026-01-30" not in synced
    assert "2026-02-01" in synced
    assert "2026-02-28" in synced
    assert "2026-02-29" not in synced
    assert list(remote.iterdir()) == []
    assert list(local.iterdir()) == []


def test_window_crosses_leap_day_and_year(tmp_path):
    remote = tmp_path / "remote"
    local = tmp_path / "local"
    remote.mkdir()
    local.mkdir()

    leap = _run(
        tmp_path, today="2024-03-01", remote=remote, local=local, dry_run=True
    )
    assert leap.returncode == 0, leap.stderr
    leap_days = _sync_lines(leap.stdout)
    assert leap_days[0] == "2024-02-01"
    assert leap_days[-1] == "2024-03-01"
    assert "2024-02-29" in leap_days
    assert len(leap_days) == 30

    year = _run(
        tmp_path,
        today="2026-01-05",
        remote=remote,
        local=local,
        days=10,
        dry_run=True,
    )
    assert year.returncode == 0, year.stderr
    year_days = _sync_lines(year.stdout)
    assert year_days[0] == "2025-12-27"
    assert year_days[-1] == "2026-01-05"
    assert len(year_days) == 10


def test_prune_selection_and_sync_contents(tmp_path):
    remote = tmp_path / "remote"
    local = tmp_path / "local"
    _write(remote / "2026-01-30" / "old.txt", "server-old")
    _write(remote / "2026-01-31" / "edge.txt", "server-edge")
    _write(remote / "2026-03-01" / "tier1" / "digest.md", "server-today")
    _write(remote / "notes" / "nope.txt", "server-notes")
    _write(remote / "stray.txt", "stray")

    _write(local / "2026-01-30" / "old.md", "local-old")
    _write(local / "2025-06-01" / "older.md", "older")
    _write(local / "2026-01-31" / "edge.md", "local-edge")
    _write(local / "2026-03-01" / "local-only.txt", "keep-me")
    _write(local / "2026-02-30" / "bad.md", "bad-day")
    _write(local / "2026-13-01" / "bad.md", "bad-month")
    _write(local / "2026-02-29" / "bad.md", "not-leap")
    _write(local / "2026-1-15" / "bad.md", "unpadded")
    _write(local / "notes" / "keep.md", "notes")
    _write(local / ".obsidian" / "workspace.json", "{}")
    _write(local / ".DS_Store", "ds")
    _write(local / "2020-01-01", "file-not-dir")
    _write(local / "2026-04-15" / "future.md", "future")
    (local / "2020-05-05").symlink_to(local / "notes", target_is_directory=True)

    result = _run(tmp_path, today="2026-03-01", remote=remote, local=local)

    assert result.returncode == 0, result.stderr + result.stdout
    assert "days synced: 30" in result.stdout.splitlines()
    assert "folders removed: 2" in result.stdout.splitlines()

    assert not (local / "2026-01-30").exists()
    assert not (local / "2025-06-01").exists()
    assert (local / "2026-01-31" / "edge.txt").read_text() == "server-edge"
    assert (local / "2026-01-31" / "edge.md").read_text() == "local-edge"
    assert (local / "2026-03-01" / "local-only.txt").read_text() == "keep-me"
    assert (local / "2026-03-01" / "tier1" / "digest.md").read_text() == "server-today"
    assert (local / "2026-02-30" / "bad.md").read_text() == "bad-day"
    assert (local / "2026-13-01" / "bad.md").read_text() == "bad-month"
    assert (local / "2026-02-29" / "bad.md").read_text() == "not-leap"
    assert (local / "2026-1-15" / "bad.md").read_text() == "unpadded"
    assert (local / "notes" / "keep.md").read_text() == "notes"
    assert not (local / "notes" / "nope.txt").exists()
    assert (local / ".obsidian" / "workspace.json").read_text() == "{}"
    assert (local / ".DS_Store").read_text() == "ds"
    assert (local / "2020-01-01").read_text() == "file-not-dir"
    assert (local / "2026-04-15" / "future.md").read_text() == "future"
    assert (local / "2020-05-05").is_symlink()
    assert not (local / "stray.txt").exists()

    assert (remote / "2026-01-30" / "old.txt").read_text() == "server-old"
    assert (remote / "notes" / "nope.txt").read_text() == "server-notes"
    assert (remote / "stray.txt").read_text() == "stray"


def test_dry_run_changes_nothing(tmp_path):
    remote = tmp_path / "remote"
    local = tmp_path / "local"
    _write(remote / "2026-03-01" / "new.txt", "new")
    _write(local / "2026-01-01" / "old.txt", "old")
    _write(local / ".obsidian" / "workspace.json", "{}")

    result = _run(
        tmp_path, today="2026-03-01", remote=remote, local=local, dry_run=True
    )

    assert result.returncode == 0, result.stderr
    synced = _sync_lines(result.stdout)
    assert synced[0] == "2026-01-31"
    assert "2026-03-01" in synced
    assert "2026-01-01" not in synced
    assert "would delete: 2026-01-01" in result.stdout.splitlines()
    assert "would delete: .obsidian" not in result.stdout
    assert (local / "2026-01-01" / "old.txt").read_text() == "old"
    assert not (local / "2026-03-01").exists()
    assert (remote / "2026-03-01" / "new.txt").read_text() == "new"
    assert (local / ".obsidian" / "workspace.json").read_text() == "{}"


def test_missing_env_vars_fail(tmp_path):
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    _write(local / "2020-01-01" / "keep.txt", "stay")
    remote.mkdir()

    missing_remote = _run(
        tmp_path,
        today="2026-03-01",
        remote=remote,
        local=local,
        env_overrides={"JD_SYNC_REMOTE": None},
    )
    assert missing_remote.returncode != 0
    assert "JD_SYNC_REMOTE" in missing_remote.stderr
    assert (local / "2020-01-01" / "keep.txt").read_text() == "stay"

    missing_local = _run(
        tmp_path,
        today="2026-03-01",
        remote=remote,
        local=local,
        env_overrides={"JD_SYNC_LOCAL": None},
    )
    assert missing_local.returncode != 0
    assert "JD_SYNC_LOCAL" in missing_local.stderr
    assert (local / "2020-01-01" / "keep.txt").read_text() == "stay"


def test_failed_sync_skips_prune(tmp_path):
    local = tmp_path / "local"
    _write(local / "2020-01-01" / "keep.txt", "stay")
    _write(local / ".obsidian" / "workspace.json", "{}")
    missing_remote = tmp_path / "no-such-remote"

    result = _run(
        tmp_path, today="2026-03-01", remote=missing_remote, local=local
    )

    assert result.returncode != 0
    assert "sync failed" in result.stderr
    assert (local / "2020-01-01" / "keep.txt").read_text() == "stay"
    assert (local / ".obsidian" / "workspace.json").read_text() == "{}"
    assert "days synced:" not in result.stdout
    assert "folders removed:" not in result.stdout
