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


def _send_report_messages(label: str, messages: list[str]) -> None:
    """Send each message in order. A failed send is logged, never raised."""
    failed = []
    for index, message in enumerate(messages, start=1):
        try:
            if send_message(message, os.environ["TELEGRAM_TOKEN"],
                            os.environ["TELEGRAM_CHAT_ID"], parse_mode=None) is False:
                failed.append(index)
        except Exception as e:
            failed.append(index)
            print(f"[orchestrator] {label} message {index} FAILED: "
                  f"{type(e).__name__}: {telemetry._redact(str(e))}")
    if failed:
        listed = ", ".join(str(n) for n in failed)
        print(f"[orchestrator] {label} incomplete: message(s) {listed} of {len(messages)} failed.")
    else:
        print(f"[orchestrator] {label} sent ({len(messages)} message"
              f"{'s' if len(messages) != 1 else ''}).")


def _send_tier_report(tier: int, daily_run_id: str) -> None:
    """One tier's report, sent right after that tier's output message.

    Never raises and never re-runs the tier: at this point the tier's work and
    its output message are already done, so a report failure is only a log line.
    """
    try:
        load_dotenv()
        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            print(f"[orchestrator] Tier {tier} report skipped: DATABASE_URL is not set.")
            return
        telemetry.drain_buffers(telemetry.buffer_dir(), db_url)  # land any stragglers first
        settings = report_data.load_tier_settings(CONFIGS)
        report = report_data.load_day(db_url, settings=settings, daily_run_id=daily_run_id,
                                      finished_through=tier)
        messages = report_render.render_tier_messages(report, tier, settings=settings)
        _send_report_messages(f"Tier {tier} report", messages)
    except Exception as e:
        print(f"[orchestrator] Tier {tier} report FAILED: {type(e).__name__}: "
              f"{telemetry._redact(str(e))}")


def _send_day_summary(daily_run_id: str) -> None:
    """Message 9: the day summary and per-tier totals. Never raises: a report
    failure must never turn a finished run into a failed one."""
    try:
        load_dotenv()
        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            print("[orchestrator] Day summary skipped: DATABASE_URL is not set.")
            return
        telemetry.drain_buffers(telemetry.buffer_dir(), db_url)  # land any stragglers first
        settings = report_data.load_tier_settings(CONFIGS)
        report = report_data.load_day(db_url, settings=settings, daily_run_id=daily_run_id,
                                      finished_through=len(CONFIGS))
        messages = report_render.render_summary_messages(report, settings=settings)
        _send_report_messages("Day summary", messages)
    except Exception as e:
        print(f"[orchestrator] Day summary FAILED: {type(e).__name__}: {telemetry._redact(str(e))}")


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

        # The tier's output message is already sent by main.py. Its report goes
        # out now, before the cooldown, so the pair arrives together.
        _send_tier_report(tier, daily_run_id)

        if tier < len(CONFIGS):
            print(f"[orchestrator] Cooling down {COOLDOWN_SECONDS // 60}m before Tier {tier + 1}...")
            time.sleep(COOLDOWN_SECONDS)

    print(f"[orchestrator] All tiers complete at {_now()}")
    _send_day_summary(daily_run_id)


if __name__ == "__main__":
    main()
