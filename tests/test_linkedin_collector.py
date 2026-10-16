from datetime import datetime, timedelta, timezone

from src.linkedin.catalog import CollectorConfig, SliceSpec, sync_slices
from src.linkedin.collector import collect_once, make_tick, sync_and_clamp
from src.linkedin.pace import Gate
from tests.linkedin_fakes import FakePace, ScriptedGet, ids, page_html, response

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
SAT = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
H = 3600
CFG = CollectorConfig(due_hours={1: 4, 3: 12}, legacy_lock_path=None)


def _slices(conn, *specs):
    sync_slices(conn, list(specs), NOW)
    conn.commit()


def test_collect_once_walks_the_most_urgent_slice_and_schedules_it(store_conn):
    _slices(store_conn, SliceSpec("linkedin", "AI Engineer", "Germany", 3, 43200),
            SliceSpec("linkedin", "AI Engineer", "Italy", 1, 14400))
    get = ScriptedGet(response(200, page_html(*ids(0, 4))), response(200, page_html()))
    tick = collect_once(store_conn, FakePace(), CFG, now=NOW, get=get, sleep=lambda _s: None)
    assert tick.did_work and tick.detail["slice"] == "AI Engineer / Italy"
    assert get.calls[0]["location"] == "Italy"
    with store_conn.cursor() as cur:
        cur.execute("SELECT area, last_walked_at, due_at > %s FROM slices ORDER BY tier", (NOW,))
        assert cur.fetchall() == [("Italy", NOW, True), ("Germany", None, False)]
        cur.execute("SELECT count(*) FROM jobs WHERE stage = 'seen' AND description_status = 'pending'")
        assert cur.fetchone()[0] == 4


def test_collect_once_with_nothing_due_does_nothing(store_conn):
    _slices(store_conn, SliceSpec("linkedin", "AI Engineer", "Italy", 1, 14400))
    with store_conn.cursor() as cur:
        cur.execute("UPDATE slices SET due_at = %s", (NOW + timedelta(hours=1),))
    get = ScriptedGet()
    tick = collect_once(store_conn, FakePace(), CFG, now=NOW, get=get, sleep=lambda _s: None)
    assert not tick.did_work and get.calls == []


def test_the_collector_waits_while_the_legacy_cron_run_holds_its_lock(store_conn, tmp_path):
    import fcntl
    lock = tmp_path / "lock"
    lock.write_text("")
    _slices(store_conn, SliceSpec("linkedin", "AI Engineer", "Italy", 1, 14400))
    cfg = CollectorConfig(due_hours={1: 4}, legacy_lock_path=str(lock))
    get = ScriptedGet(response(200, page_html()))
    tick_fn = make_tick(FakePace(), cfg, get=get, sleep=lambda _s: None)
    with open(lock, "a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        blocked = tick_fn(store_conn)
    assert not blocked.did_work and "legacy" in blocked.detail["waiting"] and get.calls == []
    assert tick_fn(store_conn).did_work  # the lock is free again


def test_a_poison_slice_is_isolated_and_the_next_slice_is_walked(store_conn):
    from unittest import mock
    _slices(store_conn, SliceSpec("linkedin", "AI Engineer", "Italy", 1, 14400),
            SliceSpec("linkedin", "ML Engineer", "Germany", 3, 43200))
    with mock.patch("src.linkedin.walk.record_sighting", side_effect=ValueError("poison")):
        tick = collect_once(store_conn, FakePace(), CFG, now=NOW,
                            get=ScriptedGet(response(200, page_html(*ids(0, 3)))),
                            sleep=lambda _s: None)
    assert tick.did_work and tick.detail["stop_reason"] == "error"
    next_tick = collect_once(store_conn, FakePace(), CFG, now=NOW + timedelta(minutes=1),
                             get=ScriptedGet(response(200, page_html())), sleep=lambda _s: None)
    assert next_tick.detail["slice"] == "ML Engineer / Germany"


def test_a_closed_schedule_gate_makes_the_tick_idle_without_a_request(store_conn):
    _slices(store_conn, SliceSpec("linkedin", "AI Engineer", "Italy", 1, 14400))
    gate = Gate(open=False, reason="closed_hours", requests_today=0, cap=1400, window="weekday")
    get = ScriptedGet()
    tick = collect_once(store_conn, FakePace(gate=gate), CFG, now=NOW, get=get, sleep=lambda _s: None)
    assert not tick.did_work and tick.detail["waiting"] == "linkedin closed_hours"
    assert get.calls == []


def test_the_weekend_walks_a_due_daily_slice(store_conn):
    _slices(store_conn, SliceSpec("linkedin", "AI Engineer", "Switzerland", 2, 24 * 3600))
    get = ScriptedGet(response(200, page_html(*ids(0, 2))), response(200, page_html()))
    tick = collect_once(store_conn, FakePace(), CFG, now=SAT, get=get, sleep=lambda _s: None)
    assert tick.did_work and tick.detail["slice"] == "AI Engineer / Switzerland"


def test_startup_sync_also_clamps_the_cadence(store_conn):
    sync_slices(store_conn, [SliceSpec("linkedin", "AI Engineer", "Italy", 1, 12 * H)], NOW)
    with store_conn.cursor() as cur:
        cur.execute("UPDATE slices SET interval_seconds = 3600")
    store_conn.commit()
    counts = sync_and_clamp(store_conn, [SliceSpec("linkedin", "AI Engineer", "Italy", 1, 12 * H)], CFG)
    store_conn.commit()
    assert counts["clamped"] == 1
    with store_conn.cursor() as cur:
        cur.execute("SELECT interval_seconds FROM slices")
        assert cur.fetchone() == (12 * H,)


def test_a_collector_tick_reuses_one_session(monkeypatch, store_conn):
    from types import SimpleNamespace
    made = []
    monkeypatch.setattr("src.linkedin.collector.new_session",
                        lambda: made.append(1) or SimpleNamespace(get=ScriptedGet()))
    tick = make_tick(FakePace(), CFG)
    tick(store_conn)
    tick(store_conn)
    assert len(made) == 1
