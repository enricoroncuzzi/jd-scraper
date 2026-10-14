"""Never let the new collector and the old cron run touch LinkedIn together.

The cron wrapper (`jds-daily.sh`) holds an flock on a lock file for the whole
run. The collector asks, without waiting, whether that lock is held.
"""
import fcntl
import os


def legacy_run_active(lock_path: str | None) -> bool:
    if not lock_path or not os.path.exists(lock_path):
        return False
    with open(lock_path, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
    return False
