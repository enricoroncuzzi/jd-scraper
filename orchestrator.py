import os
import subprocess
import sys
import time
import uuid
from datetime import datetime

from dotenv import load_dotenv

from src import telemetry, report_data, report_render
from src.telegram import send_message

CONFIGS = [
    "config/config_tier1.json",
    "config/config_tier2.json",
    "config/config_tier3.json",
    "config/config_tier4.json",
]

COOLDOWN_SECONDS = 1200  # 20 minutes between tiers


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _send_morning_report(daily_run_id: str) -> None:
    """The captain's morning health report. Never raises: a report failure must
    never turn a finished run into a failed one."""
    try:
        load_dotenv()
        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            print("[orchestrator] Morning report skipped: DATABASE_URL is not set.")
            return
        telemetry.drain_buffers(telemetry.buffer_dir(), db_url)  # land any stragglers first
        settings = report_data.load_tier_settings(CONFIGS)
        report = report_data.load_day(db_url, settings=settings, daily_run_id=daily_run_id)
        messages = report_render.render_messages(report, settings=settings)
        failed = []
        for index, message in enumerate(messages, start=1):
            try:
                if send_message(message, os.environ["TELEGRAM_TOKEN"],
                                os.environ["TELEGRAM_CHAT_ID"], parse_mode=None) is False:
                    failed.append(index)
            except Exception as e:
                failed.append(index)
                print(f"[orchestrator] Morning report message {index} FAILED: "
                      f"{type(e).__name__}: {telemetry._redact(str(e))}")
        if failed:
            listed = ", ".join(str(n) for n in failed)
            print(f"[orchestrator] Morning report incomplete: message(s) {listed} "
                  f"of {len(messages)} failed.")
        else:
            print(f"[orchestrator] Morning report sent ({len(messages)} message{'s' if len(messages) != 1 else ''}).")
    except Exception as e:
        print(f"[orchestrator] Morning report FAILED: {type(e).__name__}: {telemetry._redact(str(e))}")


def main() -> None:
    print(f"[orchestrator] Daily run started at {_now()}")
    daily_run_id = uuid.uuid4().hex
    env = {**os.environ, "JDS_DAILY_RUN_ID": daily_run_id}
    for i, config in enumerate(CONFIGS):
        tier = i + 1
        print(f"[orchestrator] Starting Tier {tier} at {_now()}")
        t0 = time.monotonic()
        result = subprocess.run([sys.executable, "-u", "main.py", config], env=env)
        elapsed = int(time.monotonic() - t0)
        mins, secs = divmod(elapsed, 60)
        if result.returncode == 0:
            print(f"[orchestrator] Tier {tier} done in {mins}m {secs}s")
        else:
            print(f"[orchestrator] Tier {tier} FAILED (exit {result.returncode}) in {mins}m {secs}s — continuing")

        if tier < len(CONFIGS):
            print(f"[orchestrator] Cooling down {COOLDOWN_SECONDS // 60}m before Tier {tier + 1}...")
            time.sleep(COOLDOWN_SECONDS)

    print(f"[orchestrator] All tiers complete at {_now()}")
    _send_morning_report(daily_run_id)


if __name__ == "__main__":
    main()
