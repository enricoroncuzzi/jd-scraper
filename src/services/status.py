"""Operator view of the job store, for the person running the services.

    python -m src.services.status

Every timestamp is printed in Europe/Rome and labelled, whatever zone the server
or the database session happens to be in (design section 4.7).
"""
import sys
from datetime import datetime

from dotenv import load_dotenv

from src.jobstore import budget, heartbeat
from src.jobstore.db import connect, store_url, utcnow
from src.linkedin.pace import Gate, PaceState, evaluate_gate, stored_spacing
from src.linkedin.schedule import ScheduleConfig, load_schedule_config, requests_today_for


def rome(value: datetime | None, schedule: ScheduleConfig, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """A timestamp in Europe/Rome, labelled. None prints as `never`."""
    if value is None:
        return "never"
    return f"{value.astimezone(schedule.zone):{fmt}} Rome"


def _gate_line(gate: Gate, schedule: ScheduleConfig) -> str:
    left = f"{gate.requests_today}/{gate.cap} requests today ({gate.window})"
    if gate.reason == "closed_hours":
        return f"LinkedIn gate: {left}, window closed until {schedule.closed_hours[1]:02d}:00 Rome"
    if gate.reason == "daily_cap":
        return f"LinkedIn gate: {left}, daily cap reached"
    return f"LinkedIn gate: {left}, window open"


def render(conn, now: datetime, schedule: ScheduleConfig | None = None) -> str:
    schedule = schedule or load_schedule_config()
    lines = []
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(tier::text, '-'), stage, count(*), min(first_seen_at) FROM jobs "
                    "GROUP BY 1, 2 ORDER BY 1, 2")
        lines.append("Jobs by tier and stage (oldest first_seen):")
        for tier, stage, count, oldest in cur.fetchall():
            lines.append(f"  tier {tier:<2} {stage:<13} {count:>6}   {rome(oldest, schedule)}")
        cur.execute("SELECT source, count(*) FILTER (WHERE active), count(*) FILTER (WHERE active AND due_at <= %s), "
                    "max(last_walked_at) FROM slices GROUP BY source ORDER BY source", (now,))
        lines.append("Slices (active, due now, last walk):")
        for source, active, due, last in cur.fetchall():
            lines.append(f"  {source:<10} {active:>4} {due:>4}   {rome(last, schedule)}")
        cur.execute("SELECT source, base_interval_seconds, count(*) FROM slices WHERE active "
                    "GROUP BY 1, 2 ORDER BY 1, 2")
        cadence: dict[str, list[str]] = {}
        for source, seconds, count in cur.fetchall():
            cadence.setdefault(source, []).append(f"{seconds / 3600:g}h x{count}")
        for source, parts in cadence.items():
            lines.append(f"  {source:<10} cadence: " + ", ".join(parts))
        cur.execute("SELECT next_slot_at, cooldown_until, last_kind, wanting, requests_today, "
                    "refusals_today, day, refusal_streak, request_times, refusal_times, nudge, "
                    "nudged_at, weekday_spacing_seconds, weekend_spacing_seconds, calm_days, "
                    "last_clean_at FROM pace_budgets WHERE host = 'linkedin.com'")
        pace = cur.fetchone()
        if pace:
            (next_slot, wait_until, kind, wanting, today, refusals, day, streak, request_times,
             refusal_times, nudge, nudged_at, weekday_spacing, weekend_spacing, calm_days,
             last_clean_at) = pace
            state = PaceState(next_slot, kind, dict(wanting or {}), today, refusals, day, wait_until,
                              streak, list(request_times or []), list(refusal_times or []), nudge,
                              nudged_at, weekday_spacing, weekend_spacing, calm_days, last_clean_at)
            wd = stored_spacing(state, schedule, "weekday")
            we = stored_spacing(state, schedule, "weekend")
            success = (f", last success {rome(last_clean_at, schedule)}" if last_clean_at else "")
            waiting = (f", waiting until {rome(wait_until, schedule, '%H:%M:%S')}"
                       if wait_until and wait_until > now else "")
            lines.append(f"LinkedIn pace: base {wd:.0f}s weekday / {we:.0f}s weekend, "
                         f"hour nudge x{state.nudge:.1f}, {state.refusal_streak} refusals in a row, "
                         f"last request: {state.last_kind}{success}{waiting}")
            gate = evaluate_gate(schedule, now,
                                 requests_today=requests_today_for(day, today, schedule, now))
            lines.append(_gate_line(gate, schedule))
    for provider in ("openrouter", "groq"):
        usage = budget.used_today(conn, provider, now)
        lines.append(f"{provider} today (UTC): {usage.requests} requests, "
                     f"{usage.input_tokens + usage.output_tokens} tokens")
    lines.append("Services:")
    for beat in heartbeat.read_all(conn):
        lines.append(f"  {beat.service:<18} {beat.state:<9} "
                     f"last beat {rome(beat.beat_at, schedule, '%Y-%m-%d %H:%M:%S')}  {beat.detail}")
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
