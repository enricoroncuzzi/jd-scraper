def test_the_docs_no_longer_describe_the_breaker_or_the_taper():
    for path in ("README.md", "deploy/RUNBOOK.md"):
        text = open(path).read()
        assert "breaker" not in text.lower() and "taper" not in text.lower()
        assert "\u2014" not in text  # the em dash


def test_the_runbook_names_every_new_config_key():
    text = open("deploy/RUNBOOK.md").read()
    for key in ("start_spacing_seconds", "max_wait_seconds", "hour_multiplier", "calm_refusal_rate",
                "min_interval_hours", "max_interval_hours", "area_max_pages", "refused_pages_to_defer",
                "description_deadline_hours", "linkedin_blocked_after_open_hours",
                "linkedin_blocked_repeat_hours", "run_budget_seconds", "jds-resume-freehire.timer",
                "00..01,05..06,11..23:00/30:00", "area_cap"):
        assert key in text


def test_every_runbook_sql_block_executes(store_conn):
    import re
    text = open("deploy/RUNBOOK.md").read()
    blocks = re.findall(r"```sql\n(.*?)```", text, re.DOTALL)
    assert len(blocks) >= 6
    with store_conn.cursor() as cur:
        for block in blocks:
            cur.execute(block)


def test_the_runbook_gap_query_ignores_pre_deploy_and_night_gaps(store_conn):
    import re
    from datetime import timedelta
    text = open("deploy/RUNBOOK.md").read()
    (gap_sql,) = [block for block in re.findall(r"```sql\n(.*?)```", text, re.DOTALL)
                  if "max(gap)" in block]
    with store_conn.cursor() as cur:
        # Pre-deploy rows have spacing_seconds NULL: a 6 h 10 m breaker gap must not count.
        cur.execute("INSERT INTO pace_events (at, kind, outcome, spacing_seconds) VALUES "
                    "('2026-10-16 08:29+02', 'search', 'ok', NULL), "
                    "('2026-10-16 14:39+02', 'search', 'ok', NULL)")
        # New-code rows 75 s apart within one Rome day; the cross-midnight pair is a different
        # partition and must not count as a gap either.
        cur.execute("INSERT INTO pace_events (at, kind, outcome, spacing_seconds) VALUES "
                    "('2026-10-17 23:59:30+02', 'search', 'ok', 75), "
                    "('2026-10-18 06:00:30+02', 'search', 'ok', 75), "
                    "('2026-10-18 06:01:45+02', 'search', 'ok', 75)")
    store_conn.commit()
    with store_conn.cursor() as cur:
        cur.execute(gap_sql)
        assert cur.fetchone()[0] == timedelta(seconds=75)
