"""Operator view of the job store, for the person running the services.

    python -m src.services.status
"""
import sys
from datetime import datetime

from dotenv import load_dotenv

from src.jobstore import budget, heartbeat
from src.jobstore.db import connect, store_url, utcnow
from src.linkedin.pace import PaceState, after_calm


def render(conn, now: datetime) -> str:
    lines = []
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(tier::text, '-'), stage, count(*), min(first_seen_at) FROM jobs "
                    "GROUP BY 1, 2 ORDER BY 1, 2")
        lines.append("Jobs by tier and stage (oldest first_seen):")
        for tier, stage, count, oldest in cur.fetchall():
            lines.append(f"  tier {tier:<2} {stage:<13} {count:>6}   {oldest:%Y-%m-%d %H:%M}")
        cur.execute("SELECT source, count(*) FILTER (WHERE active), count(*) FILTER (WHERE active AND due_at <= %s), "
                    "max(last_walked_at) FROM slices GROUP BY source ORDER BY source", (now,))
        lines.append("Slices (active, due now, last walk):")
        for source, active, due, last in cur.fetchall():
            lines.append(f"  {source:<10} {active:>4} {due:>4}   "
                         f"{last:%Y-%m-%d %H:%M}" if last else f"  {source:<10} {active:>4} {due:>4}   never")
        cur.execute("SELECT source, base_interval_seconds, count(*) FROM slices WHERE active "
                    "GROUP BY 1, 2 ORDER BY 1, 2")
        cadence: dict[str, list[str]] = {}
        for source, seconds, count in cur.fetchall():
            cadence.setdefault(source, []).append(f"{seconds / 3600:g}h x{count}")
        for source, parts in cadence.items():
            lines.append(f"  {source:<10} cadence: " + ", ".join(parts))
        cur.execute("SELECT factor, clean_streak, cooldown_until, last_kind, calm_since FROM pace_budgets "
                    "WHERE host = 'linkedin.com'")
        pace = cur.fetchone()
        if pace:
            stored, streak, cooldown, kind, calm_since = pace
            factor = after_calm(PaceState(now, stored, calm_since=calm_since), now).factor
            cooling = f", cooling down until {cooldown:%H:%M:%S}" if cooldown and cooldown > now else ""
            calm = f", no refusal since {calm_since:%H:%M}" if calm_since and factor > 1.0 else ""
            lines.append(f"LinkedIn pace: spacing x{factor:.2f} (1.00 = base), {streak} clean in a row, "
                         f"last request: {kind}{cooling}{calm}")
    for provider in ("openrouter", "groq"):
        usage = budget.used_today(conn, provider, now)
        lines.append(f"{provider} today (UTC): {usage.requests} requests, "
                     f"{usage.input_tokens + usage.output_tokens} tokens")
    lines.append("Services:")
    for beat in heartbeat.read_all(conn):
        lines.append(f"  {beat.service:<18} {beat.state:<9} last beat {beat.beat_at:%Y-%m-%d %H:%M:%S}  {beat.detail}")
    return "\n".join(lines)


def main() -> int:
    load_dotenv()
    conn = connect(store_url())
    try:
        print(render(conn, utcnow()))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
