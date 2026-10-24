"""Operator view of the job store, for the person running the services.

    python -m src.services.status

Every timestamp is printed in Europe/Rome and labelled, whatever zone the server
or the database session happens to be in (design section 4.7).
"""
import sys
from datetime import datetime, timedelta

from dotenv import load_dotenv

from src.jobstore import budget, heartbeat
from src.jobstore.db import connect, store_url, utcnow
from src.jobstore.source_requests import daily_requests
from src.llm_fallback import format_usage_lines, load_guard_config, stage_usages
from src.linkedin.adaptive import (
    climb_blockers, current_level, effective_spacing, normalize_state, parse_iso,
    paused_day, signals_from_state,
)
from src.linkedin.pace import Gate, PaceState, evaluate_gate
from src.linkedin.schedule import (
    ScheduleConfig, load_schedule_config, local_day, requests_today_for, start_spacing_seconds,
)
from src.processor.config import load_processor_config
from src.processor.describe import waiting_by_class


def rome(value: datetime | None, schedule: ScheduleConfig, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """A timestamp in Europe/Rome, labelled. None prints as `never`."""
    if value is None:
        return "never"
    return f"{value.astimezone(schedule.zone):{fmt}} Rome"


def _gate_line(gate: Gate, schedule: ScheduleConfig, state: dict | None = None) -> str:
    count = f"{gate.requests_today} requests today"
    if gate.reason == "blocked":
        reason = ((state or {}).get("paused") or {}).get("reason", "block")
        return (f"LinkedIn gate: {count}, LinkedIn paused for the rest of the day "
                f"(block persisted 1h: {reason})")
    if gate.reason == "closed_hours" and schedule.closed_hours is not None:
        return f"LinkedIn gate: {count}, window closed until {schedule.closed_hours[1]:02d}:00 Rome"
    return f"LinkedIn gate: {count}, window open"


def _pace_mode(controller: dict, schedule: ScheduleConfig) -> str:
    policy = schedule.adaptive
    spacing = effective_spacing(policy, controller, base=start_spacing_seconds(schedule))
    if not policy.enabled:
        return f"adaptive off, fixed {spacing:g}s"
    level = current_level(controller)
    if isinstance(controller, dict) and controller.get("brake"):
        return f"adaptive emergency {spacing:g}s (level {level}/5)"
    ratio = policy.levels_seconds[0] / spacing if spacing else 1.0
    return f"adaptive level {level}/5 ({spacing:g}s, {round(ratio, 1):g}x)"


def _last_move_text(controller: dict, schedule: ScheduleConfig) -> str:
    if not schedule.adaptive.enabled:
        return "disabled"
    move = controller.get("last_move") if isinstance(controller, dict) else None
    if not isinstance(move, dict):
        return "none"
    at = parse_iso(move.get("at"))
    stamp = at.astimezone(schedule.zone).strftime("%H:%M") if at else "?"
    kind = move.get("kind", "move")
    reason = move.get("reason", "")
    if kind == "brake":
        return f"emergency brake at {stamp} Rome" + (f" ({reason})" if reason else "")
    if kind == "pause":
        return f"pause at {stamp} Rome ({reason})"
    if kind == "brake cleared":
        return f"brake cleared at {stamp} Rome"
    if kind in ("drop", "climb", "demand"):
        return (f"{kind} {move.get('from')} -> {move.get('to')} at {stamp} Rome "
                f"({reason})")
    return f"{kind} at {stamp} Rome"


def _next_climb_text(controller: dict, schedule: ScheduleConfig, now: datetime) -> str:
    policy = schedule.adaptive
    if not policy.enabled:
        return ""
    if paused_day(controller) is not None:
        return "paused"
    level = current_level(controller)
    if level >= 5:
        return "top level"
    blockers = climb_blockers(policy, controller, now, signals_from_state(controller),
                              zone=schedule.zone)
    if blockers:
        return blockers[0]
    return f"ready: next request may climb to level {level + 1}"


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
                    "refusals_today, day, refusal_streak, last_clean_at, description_run, "
                    "adaptive_state FROM pace_budgets WHERE host = 'linkedin.com'")
        pace = cur.fetchone()
        if pace:
            (next_slot, wait_until, kind, wanting, today, refusals, day, streak,
             last_clean_at, description_run, adaptive_state) = pace
            controller = normalize_state(adaptive_state, schedule.adaptive)
            state = PaceState(next_slot, kind, dict(wanting or {}), today, refusals, day, wait_until,
                              streak, last_clean_at, description_run or 0, controller)
            today = requests_today_for(day, today, schedule, now)
            refusals = requests_today_for(day, refusals, schedule, now)
            window = schedule.adaptive.protect_window_requests
            trailing = len(controller["trailing"])
            trailing_refused = controller["trailing"].count("refused")
            success = (f", last success {rome(last_clean_at, schedule)}" if last_clean_at else "")
            waiting = (f", waiting until {rome(wait_until, schedule, '%H:%M:%S')}"
                       if wait_until and wait_until > now else "")
            parts = [f"LinkedIn pace: {_pace_mode(controller, schedule)}, "
                     f"{trailing_refused}/{window} refused in trailing {window}, "
                     f"{refusals}/{today} refused today, {state.refusal_streak} refusals in a row"]
            if state.last_kind:
                parts.append(f"last request: {state.last_kind}{success}{waiting}")
            elif waiting:
                parts.append(f"waiting until {rome(wait_until, schedule, '%H:%M:%S')}")
            parts.append(f"last move: {_last_move_text(controller, schedule)}")
            if schedule.adaptive.enabled:
                parts.append(f"next climb: {_next_climb_text(controller, schedule, now)}")
            lines.append(", ".join(parts))
            gate = evaluate_gate(schedule, now, requests_today=today,
                                 paused_day=(paused_day(controller)
                                             if schedule.adaptive.enabled else None))
            lines.append(_gate_line(gate, schedule, controller))
    lines.extend(_request_lines(conn, now, schedule))
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


def _request_lines(conn, now: datetime, schedule: ScheduleConfig) -> list[str]:
    """Requests and new jobs per source, one row per Rome day for the last seven days,
    today included. A source that sent nothing still gets a zero row: its silence is the
    signal. LinkedIn's rows are the pace_events rows, freehire's and Himalayas' come from
    source_requests (src/jobstore/source_requests.py)."""
    today = local_day(schedule, now)
    rows = daily_requests(conn, today - timedelta(days=6), today, zone=schedule.timezone)
    lines = ["Requests per source, last 7 days (Rome):",
             f"  {'source':<9} {'day':<10} {'sent':>6} {'ok':>6} {'refused':>7} "
             f"{'errors':>6} {'new':>5}  codes"]
    for row in rows:
        lines.append(f"  {row.source:<9} {row.day.isoformat()} {row.sent:>6} {row.ok:>6} "
                     f"{row.refused:>7} {row.errors:>6} {row.new_jobs:>5}  {row.codes_text}")
    return lines


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
