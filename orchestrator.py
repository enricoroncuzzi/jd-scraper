import subprocess
import sys
import time
from datetime import datetime

CONFIGS = [
    "config/config_tier1.json",
    "config/config_tier2.json",
    "config/config_tier3.json",
    "config/config_tier4.json",
]

COOLDOWN_SECONDS = 1200  # 20 minutes between tiers


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def main() -> None:
    print(f"[orchestrator] Daily run started at {_now()}")
    for i, config in enumerate(CONFIGS):
        tier = i + 1
        print(f"[orchestrator] Starting Tier {tier} at {_now()}")
        t0 = time.monotonic()
        result = subprocess.run([sys.executable, "-u", "main.py", config])
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


if __name__ == "__main__":
    main()
