"""The systemd units are code too: pin the properties the design relies on."""
import configparser
import glob
import importlib
import json
import os
import re
import subprocess

import pytest

UNITS = sorted(glob.glob("deploy/systemd/*.service"))

# The box, PostgreSQL's own working set, the operating system's share (kernel, systemd,
# sshd, the watchdog oneshot and a pg_dump), and memory reserved for anything else that
# shares the server. The captain's decision: jd-scraper has the whole server for now (the
# other project is expected to get its own), so OTHER_WORKLOAD_MB is 0 and the ceilings are
# sized for jd-scraper, PostgreSQL and the OS alone. Raise a reserve if that changes, and
# re-measure before trusting these first-guess ceilings.
BOX_MB = 1900
POSTGRES_MB = 400
SYSTEM_RESERVE_MB = 250
OTHER_WORKLOAD_MB = 0


def _read(path):
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read(path)
    return parser


def _is_oneshot(path):
    return _read(path)["Service"].get("Type", "").lower() == "oneshot"


# The watchdog, the backup and the stage-2 collectors (run by their own timers) are oneshot
# units; they are not part of the always-on services' shared memory budget.
LONG_RUNNING = [u for u in UNITS if not _is_oneshot(u)]

PYTHON_UNITS = [u for u in UNITS if "-m" in _read(u)["Service"]["ExecStart"]]


def test_every_expected_unit_exists():
    names = {os.path.basename(u) for u in UNITS}
    assert {"jds-collect-linkedin.service", "jds-process.service", "jds-deliver.service",
            "jds-watchdog.service", "jds-backup.service", "jds-collect-freehire.service",
            "jds-collect-himalayas.service"} <= names
    assert os.path.exists("deploy/systemd/jds-watchdog.timer")
    assert os.path.exists("deploy/systemd/jds-backup.timer")
    assert os.path.exists("deploy/systemd/jds-collect-freehire.timer")
    assert os.path.exists("deploy/systemd/jds-collect-himalayas.timer")


@pytest.mark.parametrize("path", LONG_RUNNING)
def test_long_running_services_restart_on_their_own_run_as_the_deploy_user_and_are_memory_capped(path):
    service = _read(path)["Service"]
    assert service["Restart"] == "always" and service["User"] == "enrico"
    assert service["WorkingDirectory"] == "/home/enrico/jd-scraper"
    assert service["MemoryMax"].endswith(("M", "G")) and service["MemoryHigh"].endswith(("M", "G"))
    assert int(service["TimeoutStopSec"]) >= 120  # SIGTERM is honoured between ticks, which can take minutes
    assert "postgresql.service" in _read(path)["Unit"]["Requires"]


@pytest.mark.parametrize("path", PYTHON_UNITS)
def test_every_exec_start_points_at_an_importable_module(path):
    command = _read(path)["Service"]["ExecStart"].split()
    module = command[command.index("-m") + 1]
    importlib.import_module(module)
    assert command[0] == "/home/enrico/jd-scraper/.venv/bin/python"


def test_total_memory_ceiling_leaves_room_for_postgres_and_the_other_project():
    def megabytes(value):
        return int(value[:-1]) * (1024 if value.endswith("G") else 1)
    total = sum(megabytes(_read(u)["Service"]["MemoryMax"]) for u in LONG_RUNNING)
    assert total <= BOX_MB - POSTGRES_MB - SYSTEM_RESERVE_MB - OTHER_WORKLOAD_MB


# Timer-driven collectors are the stage-2 sources; the LinkedIn collector has no timer and
# stays a long-running service. Deriving the services from the timers keeps this general
# when a later plan adds another source.
COLLECTOR_TIMERS = sorted(glob.glob("deploy/systemd/jds-collect-*.timer"))
COLLECTOR_SERVICES = [timer[: -len(".timer")] + ".service" for timer in COLLECTOR_TIMERS]


def _daily_start_minutes(timer_path):
    value = _read(timer_path)["Timer"]["OnCalendar"]
    day, clock = value.split()[:2]
    assert day == "*-*-*", f"{timer_path}: expected a daily timer, got {value!r}"
    hour, minute = (int(part) for part in clock.split(":")[:2])
    return hour * 60 + minute


def _delivery_minutes():
    value = json.load(open("config/deliver.json"))["delivery_time"]
    hour, minute = (int(part) for part in value.split(":"))
    return hour * 60 + minute


def test_oneshot_collectors_run_on_timers_clear_of_delivery_and_each_other():
    # Every extra source is a oneshot unit on its own daily timer, so its small cap never
    # coincides with the delivery/Chromium peak or with another collector.
    assert COLLECTOR_TIMERS, "a stage-2 collector timer must exist"
    for service in COLLECTOR_SERVICES:
        assert os.path.exists(service), f"{service} has a timer but no service"
        assert "Install" not in _read(service), "an enabled oneshot would run at boot, outside its timer"
        service_cfg = _read(service)["Service"]
        assert service_cfg["Type"] == "oneshot" and "Restart" not in service_cfg
        assert "--once" in service_cfg["ExecStart"]
    starts = [_daily_start_minutes(timer) for timer in COLLECTOR_TIMERS]
    assert len(starts) == len(set(starts)), f"collector timers overlap: {starts}"
    delivery = _delivery_minutes()
    for start in starts:
        assert not (delivery - 60 <= start < delivery + 180), (
            f"collector start {start} falls in the delivery window around {delivery}")
    for earlier, later in zip(sorted(starts), sorted(starts)[1:]):
        assert later - earlier >= 60, f"collectors run too close together: {sorted(starts)}"


BACKUP_SCRIPT = "deploy/backup-jobstore.sh"


def _stub_pg_dump(tmp_path, rc=0):
    """A fake pg_dump that writes the file named by --file and exits with rc."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "pg_dump"
    stub.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do case "$arg" in --file=*) out="${arg#--file=}";; esac; done\n'
        'echo dump > "$out"\n'
        f"exit {rc}\n"
    )
    stub.chmod(0o755)
    return bindir


def _run_backup(backup_dir, bindir):
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["JDS_BACKUP_DIR"] = str(backup_dir)
    env["JOBSTORE_URL"] = "postgresql:///jds"
    return subprocess.run([BACKUP_SCRIPT], capture_output=True, text=True, env=env)


def test_the_job_store_backup_runs_nightly_and_keeps_seven_days(tmp_path):
    timer = _read("deploy/systemd/jds-backup.timer")["Timer"]
    assert "OnCalendar" in timer
    assert _read("deploy/systemd/jds-backup.service")["Service"]["Type"] == "oneshot"

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    old = []
    for index in range(9):
        dump = backup_dir / f"jobstore-old{index}.dump"
        dump.write_text("old")
        os.utime(dump, (1_000_000 + index, 1_000_000 + index))  # old8 is the newest pre-existing
        old.append(dump.name)

    result = _run_backup(backup_dir, _stub_pg_dump(tmp_path))
    assert result.returncode == 0, result.stderr

    # Retention is by count, not by mtime: 9 old plus 1 fresh leaves exactly 7.
    kept = sorted(p.name for p in backup_dir.glob("jobstore-*.dump"))
    assert len(kept) == 7, kept
    fresh = sorted(set(kept) - set(old))
    assert len(fresh) == 1, f"the just-written dump must be the newest kept: {kept}"
    for index in range(3):  # the three oldest pre-existing dumps are pruned
        assert f"jobstore-old{index}.dump" not in kept


def test_the_default_backup_directory_is_outside_the_deploy_repo():
    # A default inside /home/enrico/jd-scraper would be staged by `git add -A` and wiped by
    # `git clean -fdx` during a deploy; the dumps belong outside the working tree.
    match = re.search(r"JDS_BACKUP_DIR:-([^}]*)\}", open(BACKUP_SCRIPT).read())
    assert match, "the backup script must read JDS_BACKUP_DIR with a default"
    default = os.path.abspath(match.group(1))
    assert os.path.isabs(default)
    deploy_repo = "/home/enrico/jd-scraper"
    assert os.path.commonpath([deploy_repo, default]) != deploy_repo, (
        f"default backup dir {default} lives inside {deploy_repo}")


def test_a_failed_dump_leaves_no_partial_and_no_new_backup(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    (backup_dir / "jobstore-old.dump").write_text("old")

    result = _run_backup(backup_dir, _stub_pg_dump(tmp_path, rc=1))
    assert result.returncode != 0
    # A dump that dies midway must not leave a file behind to be counted later.
    assert sorted(p.name for p in backup_dir.iterdir()) == ["jobstore-old.dump"]


def test_the_backup_script_named_by_the_unit_is_executable():
    # systemd runs ExecStart as an exec, not through a shell, so a committed mode of
    # 100644 makes jds-backup.service exit 203/EXEC every night with no dump written.
    target = _read("deploy/systemd/jds-backup.service")["Service"]["ExecStart"].split()[0]
    script_path = target.replace("/home/enrico/jd-scraper/", "")
    assert os.path.exists(script_path), f"{target} does not exist in the checkout"
    assert os.access(script_path, os.X_OK), f"{target} is not executable (systemd reports 203/EXEC)"


@pytest.mark.parametrize("source", ["freehire", "himalayas"])
def test_the_run_budget_ends_before_the_unit_timeout(source):
    budget = json.load(open("config/sources.json"))[source]["throttle"]["run_budget_seconds"]
    timeout = int(_read(f"deploy/systemd/jds-collect-{source}.service")["Service"]["TimeoutStartSec"])
    assert budget < timeout


RESUME_TIMERS = sorted(glob.glob("deploy/systemd/jds-resume-*.timer"))


def _expand_hours(spec):
    """'00..01,05..06,11..23' -> {0, 1, 5, 6, 11, 12, ..., 23}"""
    hours = set()
    for part in spec.split(","):
        first, _, last = part.partition("..")
        hours.update(range(int(first), int(last or first) + 1))
    return hours


def _resume_slots(timer):
    """Every start of a resume timer, in minutes after midnight."""
    hours_spec, minutes = _read(timer)["Timer"]["OnCalendar"].split()[1].split(":")[:2]
    assert minutes == "00/30", f"{timer}: a resume run must fire every 30 minutes"
    return {hour * 60 + minute for hour in _expand_hours(hours_spec) for minute in (0, 30)}


def test_resume_runs_are_oneshot_every_half_hour_outside_the_delivery_window():
    assert {os.path.basename(t) for t in RESUME_TIMERS} == {"jds-resume-freehire.timer", "jds-resume-himalayas.timer"}
    delivery = _delivery_minutes()
    daily_starts = [_daily_start_minutes(t) for t in COLLECTOR_TIMERS]
    for timer in RESUME_TIMERS:
        slots = _resume_slots(timer)
        assert not [s for s in slots if delivery - 60 <= s < delivery + 180], "07:00-10:59 must stay clear"
        for start in daily_starts:
            # A resume run holding the per-source lock at a daily start would make the daily
            # run exit without walking that day's slices (review change 2).
            clash = sorted(s for s in slots if start - 60 <= s < start + 60)
            assert not clash, f"{timer} fires within an hour of the daily run at {start}: {clash}"
        service_path = timer[: -len(".timer")] + ".service"
        service = _read(service_path)["Service"]
        assert service["Type"] == "oneshot" and "--resume" in service["ExecStart"]
        assert int(service["TimeoutStartSec"]) <= 1800 and "Install" not in _read(service_path)
