"""The systemd units are code too: pin the properties the design relies on."""
import configparser
import glob
import importlib
import os

import pytest

UNITS = sorted(glob.glob("deploy/systemd/*.service"))
# The watchdog and the backup are oneshot units, not long-running services.
LONG_RUNNING = [u for u in UNITS if "watchdog" not in u and "backup" not in u]

# The box, PostgreSQL's own working set, and memory reserved for anything else that
# shares the server. The captain's decision: jd-scraper has the whole server for now
# (the other project is expected to get its own), so the reserve is 0 and the
# ceilings are sized for jd-scraper plus PostgreSQL alone. Raise the reserve if that
# changes, and re-measure before trusting these first-guess ceilings.
BOX_MB = 1900
POSTGRES_MB = 400
OTHER_WORKLOAD_MB = 0


def _read(path):
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read(path)
    return parser


PYTHON_UNITS = [u for u in UNITS if "-m" in _read(u)["Service"]["ExecStart"]]


def test_every_expected_unit_exists():
    names = {os.path.basename(u) for u in UNITS}
    assert {"jds-collect-linkedin.service", "jds-process.service", "jds-deliver.service",
            "jds-watchdog.service", "jds-backup.service"} <= names
    assert os.path.exists("deploy/systemd/jds-watchdog.timer")
    assert os.path.exists("deploy/systemd/jds-backup.timer")


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
    assert total <= BOX_MB - POSTGRES_MB - OTHER_WORKLOAD_MB


def test_the_job_store_backup_runs_nightly_and_keeps_seven_days():
    timer = _read("deploy/systemd/jds-backup.timer")["Timer"]
    assert "OnCalendar" in timer
    assert _read("deploy/systemd/jds-backup.service")["Service"]["Type"] == "oneshot"
    script = open("deploy/backup-jobstore.sh").read()
    assert "pg_dump" in script and "-mtime +7" in script


def test_the_backup_script_named_by_the_unit_is_executable():
    # systemd runs ExecStart as an exec, not through a shell, so a committed mode of
    # 100644 makes jds-backup.service exit 203/EXEC every night with no dump written.
    target = _read("deploy/systemd/jds-backup.service")["Service"]["ExecStart"].split()[0]
    script_path = target.replace("/home/enrico/jd-scraper/", "")
    assert os.path.exists(script_path), f"{target} does not exist in the checkout"
    assert os.access(script_path, os.X_OK), f"{target} is not executable (systemd reports 203/EXEC)"
