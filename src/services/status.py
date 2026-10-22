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
from src.llm_fallback import format_usage_lines, load_guard_config, stage_usages
from src.linkedin.pace import Gate, PaceState, evaluate_gate
from src.linkedin.schedule import ScheduleConfig, load_schedule_config, requests_today_for, start_spacing_seconds
from src.processor.config import load_processor_config
from src.processor.describe import waiting_by_class


def rome(value: datetime | None, schedule: ScheduleConfig, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """A timestamp in Europe/Rome, labelled. None prints as `never`."""
    if value is None:
        return "never"
    return f"{value.astimezone(schedule.zone):{fmt}} Rome"


def _gate_line(gate: Gate, schedule: ScheduleConfig) -> str:
    state = f"{gate.requests_today} requests today"
    if gate.reason == "closed_hours" and schedule.closed_hours is not None:
        return f"LinkedIn gate: {state}, window closed until {schedule.closed_hours[1]:02d}:00 Rome"
    return f"LinkedIn gate: {state}, window open"


def render(conn, now: datetime, schedule: ScheduleConfig | None = None) -> str:
    schedule = schedule or load_schedule_config()
    lines = []
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(tier::text, '-'), stage, count(*), min(first_seen_at) FROM jobs "
                    "GROUP BY 1, 2 ORDER BY 1, 2")
        lines.append("Jobs by tier and stage (oldest first_seen):")
        for tier, stage, count, oldest in cur.fetchall():
            lines.append(f"  tier {tier:<2} {stage:<13} {count:>6}   {rome(oldest, schedule)}")
        try:
            cfg = load_processor_config()
        except (ValueError, OSError) as exc:
            # A malformed description_order block stops jds-process and jds-deliver at
            # start; the operator view must keep showing the gate, budgets and beats.
            lines.append(f"Description queue: the processor config is unreadable: {exc}")
        else:
            lines.extend(_description_queue_lines(cur, now, schedule, cfg))
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
                    "refusals_today, day, refusal_streak, last_clean_at, description_run "
                    "FROM pace_budgets WHERE host = 'linkedin.com'")
        pace = cur.fetchone()
        if pace:
            (next_slot, wait_until, kind, wanting, today, refusals, day, streak,
             last_clean_at, description_run) = pace
            state = PaceState(next_slot, kind, dict(wanting or {}), today, refusals, day, wait_until,
                              streak, last_clean_at, description_run or 0)
            today = requests_today_for(day, today, schedule, now)
            refusals = requests_today_for(day, refusals, schedule, now)
            base = start_spacing_seconds(schedule)
            success = (f", last success {rome(last_clean_at, schedule)}" if last_clean_at else "")
            waiting = (f", waiting until {rome(wait_until, schedule, '%H:%M:%S')}"
                       if wait_until and wait_until > now else "")
            lines.append(f"LinkedIn pace: fixed {base:.0f}s, "
                         f"{refusals}/{today} refused today, {state.refusal_streak} refusals in a row, "
                         f"last request: {state.last_kind}{success}{waiting}")
            gate = evaluate_gate(schedule, now,
                                 requests_today=today)
            lines.append(_gate_line(gate, schedule))
    for provider in ("openrouter", "groq"):
        usage = budget.used_today(conn, provider, now)
        lines.append(f"{provider} today (UTC): {usage.requests} requests, "
                     f"{usage.input_tokens + usage.output_tokens} tokens")
    # Which model actually answered, per stage (src/llm_fallback.py): OpenRouter's
    # fallback chain answers with a 200, so a broken primary is only visible here.
    lines.extend(format_usage_lines(stage_usages(conn, now, schedule.zone), load_guard_config()))
    lines.append("Services:")
    for beat in heartbeat.read_all(conn):
        lines.append(f"  {beat.service:<18} {beat.state:<9} "
                     f"last beat {rome(beat.beat_at, schedule, '%Y-%m-%d %H:%M:%S')}  {beat.detail}")
    return "\n".join(lines)


def _description_queue_lines(cur, now: datetime, schedule: ScheduleConfig, cfg) -> list[str]:
    """The waiting description queue, per tier and priority class.

    Shows the split the fetch order works on: the class each rank is, how many jobs
    wait in it and how old the tier's oldest waiting job is. A tier with nothing
    waiting still gets a line, so a tier that has gone silent is visible.
    """
    order = cfg.fetch_order
    quota = f"1 pick in {order.quota_one_in} is oldest-first" if order.enabled else "order disabled"
    names = ", ".join(cls.name for cls in order.order_for(1).classes)
    lines = [f"Description queue by priority class (tiers 1/3/4: {names}; {quota}):"]
    rows = waiting_by_class(cur, now)
    by_tier: dict[int, dict] = {}
    for tier, rank, count, oldest in rows:
        by_tier.setdefault(tier, {})[rank] = (count, oldest)
    for tier in sorted(set(by_tier) | {1, 2, 3, 4}):
        ranked = by_tier.get(tier, {})
        if not ranked:
            lines.append(f"  tier {tier:<2} (no waiting descriptions)")
            continue
        parts = [f"{order.name(tier, rank)} {ranked.get(rank, (0, None))[0]}"
                 for rank in range(len(order.order_for(tier).classes))]
        if None in ranked:
            parts.append(f"unclassified {ranked[None][0]}")
        stale = [rank for rank in ranked if rank is not None
                 and rank > len(order.order_for(tier).classes) - 1]
        for rank in sorted(stale):      # a rank left over from an older class list
            parts.append(f"{order.name(tier, rank)} {ranked[rank][0]}")
        oldest = min(oldest for _count, oldest in ranked.values() if oldest is not None)
        lines.append(f"  tier {tier:<2} " + "   ".join(parts) + f"   oldest {rome(oldest, schedule)}")
    return lines


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
