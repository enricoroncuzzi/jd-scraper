from datetime import datetime, timedelta, timezone

from src.delivery.linkedin_report import linkedin_lines
from src.linkedin.catalog import SliceSpec, load_collector_config, sync_slices
from src.linkedin.schedule import ScheduleConfig

NOW = datetime(2026, 10, 14, 6, 0, tzinfo=timezone.utc)  # 08:00 Rome, delivery time
H = 3600


def _lines(store_conn):
    return linkedin_lines(store_conn, NOW, schedule=ScheduleConfig(),
                          collector=load_collector_config())


def _event(conn, at, kind, outcome, wait=None):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO pace_events (at, service, kind, outcome, wait_seconds) "
                    "VALUES (%s, 'collect-linkedin', %s, %s, %s)", (at, kind, outcome, wait))
    conn.commit()


def test_the_block_counts_requests_refusals_and_the_longest_wait(store_conn):
    for _ in range(8):
        _event(store_conn, NOW - timedelta(hours=1), "search", "ok")
    for _ in range(2):
        _event(store_conn, NOW - timedelta(hours=1), "description", "ok")
    _event(store_conn, NOW - timedelta(hours=1), "search", "refused", 60)
    _event(store_conn, NOW - timedelta(hours=1), "description", "refused", 240)
    _event(store_conn, NOW - timedelta(hours=1), "packaging", "ok")
    _event(store_conn, NOW - timedelta(hours=25), "search", "ok")  # outside the window
    lines = _lines(store_conn)
    assert lines[0] == "LinkedIn (last 24 hours)"
    assert lines[1] == "Requests: 13 (search 9, description 3, packaging 1); refused 2 (15.4%)"
    assert lines[2] == "Longest refusal wait: 4m"


def test_the_block_shows_the_fixed_spacing(store_conn):
    with store_conn.cursor() as cur:
        # The stored old base and nudge are inert: the line reads config only.
        cur.execute("INSERT INTO pace_budgets (host, next_slot_at, weekday_spacing_seconds, nudge) "
                    "VALUES ('linkedin.com', %s, 67.5, 1.5)", (NOW,))
    store_conn.commit()
    lines = _lines(store_conn)
    assert lines[3] == "Fixed spacing: 75s"


def test_the_block_names_slices_past_48_hours(store_conn):
    sync_slices(store_conn, [
        SliceSpec("linkedin", "AI Engineer", "Italy", 1, 12 * H),
        SliceSpec("linkedin", "AI Engineer", "France", 3, 24 * H),
        SliceSpec("linkedin", "AI Engineer", "Germany", 3, 48 * H),
    ], NOW)
    with store_conn.cursor() as cur:
        cur.execute("UPDATE slices SET last_walked_at = %s WHERE area = 'Italy'", (NOW - timedelta(hours=2),))
        cur.execute("UPDATE slices SET last_walked_at = %s WHERE area = 'France'", (NOW - timedelta(hours=35),))
        cur.execute("UPDATE slices SET last_walked_at = %s WHERE area = 'Germany'", (NOW - timedelta(hours=60),))
    store_conn.commit()
    lines = _lines(store_conn)
    assert lines[4] == "Slices behind cadence: 2 of 3; past 48h+6h: AI Engineer / Germany"


def test_the_block_tolerates_six_hours_past_the_cadence(store_conn):
    sync_slices(store_conn, [SliceSpec("linkedin", "AI Engineer", "Germany", 3, 48 * H)], NOW)
    with store_conn.cursor() as cur:
        cur.execute("UPDATE slices SET last_walked_at = %s", (NOW - timedelta(hours=50),))
    store_conn.commit()
    lines = _lines(store_conn)
    assert lines[4] == "Slices behind cadence: 0 of 1; past 48h+6h: none"


def test_the_block_shows_the_oldest_job_waiting_for_a_description(store_conn):
    with store_conn.cursor() as cur:
        cur.execute("INSERT INTO jobs (canonical_url, title, company, company_norm, title_norm, "
                    "stage, tier, description_status, first_seen_at) "
                    "VALUES ('https://l/1', 'AI Engineer', 'Acme', 'acme', 'ai engineer', "
                    "'seen', 1, 'pending', %s)", (NOW - timedelta(hours=26),))
    store_conn.commit()
    lines = _lines(store_conn)
    assert lines[5] == "Oldest job waiting for a description: 26h00m (first seen 2026-10-13 06:00 Rome)"


def test_the_block_counts_area_caps_next_to_full_caps(store_conn):
    sync_slices(store_conn, [SliceSpec("linkedin", "AI Engineer", "Italy", 1, 12 * H)], NOW)
    with store_conn.cursor() as cur:
        cur.execute("SELECT id FROM slices")
        sid = cur.fetchone()[0]
        for reason, at in (("area_cap", NOW - timedelta(hours=1)), ("area_cap", NOW - timedelta(hours=2)),
                           ("cap_hit", NOW - timedelta(hours=1)), ("known_page", NOW - timedelta(hours=1)),
                           ("known_page", NOW - timedelta(hours=2)), ("known_page", NOW - timedelta(hours=3)),
                           ("area_cap", NOW - timedelta(hours=25))):
            cur.execute("INSERT INTO slice_walks (slice_id, walked_at, pages, cards_seen, new_jobs, "
                        "stop_reason) VALUES (%s, %s, 1, 1, 0, %s)", (sid, at, reason))
    store_conn.commit()
    lines = _lines(store_conn)
    assert lines[6] == "Page caps hit: area cap 2, full cap 1 (of 6 walks)"


def test_an_empty_store_gives_a_calm_block(store_conn):
    lines = _lines(store_conn)
    assert lines[1] == "Requests: 0 (search 0, description 0, packaging 0); refused 0 (0.0%)"
    assert lines[2] == "Longest refusal wait: none"
    assert lines[3] == "Fixed spacing: 75s"
    assert lines[4] == "Slices behind cadence: 0 of 0; past 48h+6h: none"
    assert lines[5] == "Oldest job waiting for a description: none"
    assert lines[6] == "Page caps hit: area cap 0, full cap 0 (of 0 walks)"
