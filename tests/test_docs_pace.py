def test_every_runbook_sql_block_executes(store_conn):
    import re
    text = open("deploy/RUNBOOK.md").read()
    blocks = re.findall(r"```sql\n(.*?)```", text, re.DOTALL)
    assert len(blocks) >= 6
    with store_conn.cursor() as cur:
        for block in blocks:
            cur.execute(block)


def test_the_runbook_gap_query_ignores_pre_deploy_rows_and_spans_midnight(store_conn):
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
        # New-code rows 75 s apart across Rome midnight: with no closed window that is a
        # real 75 s gap, so the query must no longer partition the timeline by day.
        cur.execute("INSERT INTO pace_events (at, kind, outcome, spacing_seconds) VALUES "
                    "('2026-10-17 23:59:30+02', 'search', 'ok', 75), "
                    "('2026-10-18 00:00:45+02', 'search', 'ok', 75)")
    store_conn.commit()
    with store_conn.cursor() as cur:
        cur.execute(gap_sql)
        assert cur.fetchone()[0] == timedelta(seconds=75)


def test_the_scoped_week_one_queries_assert_their_values(store_conn):
    import re
    from datetime import timedelta
    text = open("deploy/RUNBOOK.md").read()
    blocks = re.findall(r"```sql\n(.*?)```", text, re.DOTALL)

    def block(marker):
        return next(b for b in blocks if marker in b)

    pages_sql = block("avg(w.pages)")
    past_sql = block("ORDER BY last_walked_at NULLS FIRST")
    lag_sql = block("percentile_cont")
    share_sql = block("j.delivered_at - coalesce")
    with store_conn.cursor() as cur:
        # The deploy boundary is the first new-pace row: min(at) with spacing_seconds set.
        cur.execute("INSERT INTO pace_events (at, kind, outcome, spacing_seconds) VALUES "
                    "(now() - interval '6 days', 'search', 'ok', NULL), "
                    "(now() - interval '5 days', 'search', 'ok', 75), "
                    "(now() - interval '5 days' + interval '75 seconds', 'search', 'ok', 75)")
        cur.execute("INSERT INTO slices (source, role, area, tier, base_interval_seconds, "
                    "interval_seconds, last_walked_at, due_at, stop_reason, active) VALUES "
                    "('linkedin', 'AI Engineer', 'Germany', 3, 172800, 172800, now() - interval '50 hours', now(), 'end_of_results', TRUE), "
                    "('linkedin', 'AI Engineer', 'Spain', 3, 172800, 172800, now() - interval '60 hours', now(), 'end_of_results', TRUE), "
                    "('linkedin', 'AI Engineer', 'Malta', 3, 172800, 172800, NULL, now(), NULL, TRUE), "
                    "('freehire', 'ai_engineering', 'countries=it', 1, 86400, 86400, now() - interval '70 hours', now(), 'end_of_results', TRUE)")
        cur.execute("SELECT id FROM slices WHERE area = 'Germany'")
        germany = cur.fetchone()[0]
        # A pre-deploy cap_hit walk inside the 7-day window, and two post-deploy walks.
        cur.execute("INSERT INTO slice_walks (slice_id, walked_at, pages, cards_seen, new_jobs, stop_reason) VALUES "
                    "(%s, now() - interval '6 days', 40, 400, 10, 'cap_hit'), "
                    "(%s, now() - interval '2 days', 20, 200, 5, 'area_cap'), "
                    "(%s, now() - interval '1 day', 6, 60, 3, 'known_page')",
                    (germany, germany, germany))
        # A pre-deploy late delivered job, a post-deploy on-time one and a post-deploy late one.
        cur.execute("INSERT INTO jobs (canonical_url, title, company, company_norm, title_norm, stage, "
                    "first_seen_at, delivered_at) VALUES "
                    "('https://l/old', 'Old', 'A', 'a', 'old', 'delivered', now() - interval '12 days', now() - interval '6 days'), "
                    "('https://l/on', 'On', 'B', 'b', 'on', 'delivered', now() - interval '4 days', now() - interval '3 days'), "
                    "('https://l/late', 'Late', 'C', 'c', 'late', 'delivered', now() - interval '5 days', now())")
        cur.execute("INSERT INTO sightings (job_id, source, source_job_id, source_url) "
                    "SELECT id, 'linkedin', id::text, canonical_url FROM jobs "
                    "WHERE canonical_url IN ('https://l/old', 'https://l/on', 'https://l/late')")
        # The on-time job was seen twice, so a join would count it twice and inflate the share.
        cur.execute("INSERT INTO sightings (job_id, source, source_job_id, source_url) "
                    "SELECT id, 'linkedin', 'second-sighting', canonical_url FROM jobs "
                    "WHERE canonical_url = 'https://l/on'")
        # One described job, 4 h after it was first seen, and a scored job with a 9 h lag
        # that the `stage = 'described'` filter must leave out.
        cur.execute("INSERT INTO jobs (canonical_url, title, company, company_norm, title_norm, stage, "
                    "first_seen_at, stage_changed_at) VALUES "
                    "('https://l/desc', 'Desc', 'D', 'd', 'desc', 'described', now() - interval '4 hours', now()), "
                    "('https://l/scored', 'Scored', 'E', 'e', 'scored', 'scored', now() - interval '10 hours', now() - interval '1 hour')")
    store_conn.commit()
    with store_conn.cursor() as cur:
        cur.execute(pages_sql)
        row = cur.fetchone()
        assert (row[0], row[1], row[2], row[3]) == ("Germany", 2, 1, 0) and float(row[4]) == 13.0
        cur.execute(past_sql)
        assert [r[1] for r in cur.fetchall()] == ["Malta", "Spain"]
        cur.execute(lag_sql)
        assert cur.fetchone()[0] == [timedelta(hours=4)] * 3
        cur.execute(share_sql)
        assert float(cur.fetchone()[0]) == 0.5


def test_the_runbook_documents_every_description_order_key():
    import json

    block = json.load(open("config/processor.json"))["description_order"]
    text = open("deploy/RUNBOOK.md").read()
    for key in block:
        assert f"description_order.{key}" in text
    assert "description_pick_count" in text
