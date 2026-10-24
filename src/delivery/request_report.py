"""The morning report's per-source request block.

Built from the same rows the status page shows (src/jobstore/source_requests.py), one
line per source for the previous Rome day with the change in requests sent against the
day before. The day summary already carried LinkedIn's last-24-hours block; this block
is what the captain asked for: how many requests each source got that day, how each
source answered them, and how many new jobs came out of it.
"""
from datetime import datetime, timedelta

from src.jobstore.source_requests import SOURCES, daily_requests
from src.linkedin.schedule import ScheduleConfig, load_schedule_config, local_day


def request_lines(conn, now: datetime, *, schedule: ScheduleConfig | None = None) -> list[str]:
    """One line per source for the previous Rome day, plus the request count of the day
    before and the difference. A source that sent nothing keeps its line, with zeros."""
    schedule = schedule or load_schedule_config()
    today = local_day(schedule, now)
    yesterday = today - timedelta(days=1)
    before = today - timedelta(days=2)
    rows = {(row.source, row.day): row
            for row in daily_requests(conn, before, yesterday, zone=schedule.timezone)}
    lines = ["Requests per source yesterday (Rome)"]
    for source in SOURCES:
        day = rows[(source, yesterday)]
        previous = rows[(source, before)].sent
        lines.append(
            f"{source} {yesterday.isoformat()}: {day.sent} sent ({day.ok} ok, "
            f"{day.refused} refused, {day.errors} errors), {day.new_jobs} new jobs, "
            f"codes {day.codes_text}; {previous} the day before ({day.sent - previous:+d})")
    return lines
