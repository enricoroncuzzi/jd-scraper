"""The morning summary's LinkedIn block (design section 4.7).

One block, built from `pace_events`, the `pace_budgets` row, the `slices` rows and
the `jobs` table: requests and refusals, the longest refusal wait, the current base
spacing, the slices behind their cadence and past 48 h, the oldest job waiting for a
description, and the page caps this design adds.
"""
from datetime import datetime, timedelta

from src.delivery.health import age_text
from src.linkedin.catalog import CollectorConfig, load_collector_config
from src.linkedin.pace import PaceState
from src.linkedin.schedule import (
    ScheduleConfig, load_schedule_config, start_spacing_seconds,
)

WINDOW_HOURS = 24
MAX_NAMES = 5


def _pace_state(cur, now: datetime) -> PaceState:
    cur.execute(
        "SELECT next_slot_at, cooldown_until, last_kind, wanting, requests_today, refusals_today, "
        "day, refusal_streak, last_clean_at, description_run "
        "FROM pace_budgets WHERE host = 'linkedin.com'")
    row = cur.fetchone()
    if row is None:
        return PaceState(now)
    (next_slot, wait_until, last_kind, wanting, requests_today, refusals_today, day,
     refusal_streak, last_clean_at, description_run) = row
    return PaceState(next_slot, last_kind, dict(wanting or {}), requests_today, refusals_today,
                     day, wait_until, refusal_streak, last_clean_at, description_run or 0)


def _names(rows: list[tuple[str, str]]) -> str:
    names = [f"{role} / {area}" for role, area in rows[:MAX_NAMES]]
    if len(rows) > MAX_NAMES:
        names.append(f"and {len(rows) - MAX_NAMES} more")
    return ", ".join(names) if names else "none"


def linkedin_lines(conn, now: datetime, *, schedule: ScheduleConfig | None = None,
                   collector: CollectorConfig | None = None) -> list[str]:
    schedule = schedule or load_schedule_config()
    collector = collector or load_collector_config()
    since = now - timedelta(hours=WINDOW_HOURS)
    tolerance = collector.cadence_tolerance_hours * 3600
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), count(*) FILTER (WHERE kind = 'search'), "
            "count(*) FILTER (WHERE kind = 'description'), "
            "count(*) FILTER (WHERE outcome = 'refused'), max(wait_seconds) "
            "FROM pace_events WHERE at >= %s AND at < %s", (since, now))
        requests, search, description, refused, longest = cur.fetchone()
        state = _pace_state(cur, now)
        cur.execute(
            "SELECT count(*) FILTER (WHERE last_walked_at IS NULL OR "
            "last_walked_at < %s - make_interval(secs => interval_seconds + %s)), count(*) "
            "FROM slices WHERE active AND source = 'linkedin'", (now, tolerance))
        behind, active = cur.fetchone()
        cur.execute(
            "SELECT role, area FROM slices WHERE active AND source = 'linkedin' "
            "AND last_walked_at IS NOT NULL "
            "AND last_walked_at < %s - make_interval(secs => %s) ORDER BY role, area",
            (now, collector.max_interval_hours * 3600 + tolerance))
        past = cur.fetchall()
        cur.execute(
            "SELECT min(first_seen_at) FROM jobs WHERE stage = 'seen' AND tier IS NOT NULL "
            "AND description_status IN ('pending', 'rate_limited')")
        oldest = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FILTER (WHERE w.stop_reason = 'area_cap'), "
            "count(*) FILTER (WHERE w.stop_reason = 'cap_hit'), count(*) "
            "FROM slice_walks w JOIN slices s ON s.id = w.slice_id "
            "WHERE s.source = 'linkedin' AND s.active AND w.walked_at >= %s AND w.walked_at < %s",
            (since, now))
        area_cap, full_cap, walks = cur.fetchone()

    wd = start_spacing_seconds(schedule, "weekday")
    we = start_spacing_seconds(schedule, "weekend")
    pct = 100.0 * refused / requests if requests else 0.0
    refusal_wait = (f"Longest refusal wait: {age_text(timedelta(seconds=longest))}"
                    if longest is not None else "Longest refusal wait: none")
    oldest_line = ("Oldest job waiting for a description: none" if oldest is None else
                   f"Oldest job waiting for a description: {age_text(now - oldest)} "
                   f"(first seen {oldest.astimezone(schedule.zone):%Y-%m-%d %H:%M} Rome)")
    return [
        "LinkedIn (last 24 hours)",
        f"Requests: {requests} (search {search}, description {description}); "
        f"refused {refused} ({pct:.1f}%)",
        refusal_wait,
        f"Fixed spacing: {wd:.0f}s weekday, {we:.0f}s weekend",
        f"Slices behind cadence: {behind} of {active}; past {collector.max_interval_hours}h"
        f"+{collector.cadence_tolerance_hours}h: {_names(past)}",
        oldest_line,
        f"Page caps hit: area cap {area_cap}, full cap {full_cap} (of {walks} walks)",
    ]
