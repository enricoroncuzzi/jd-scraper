import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.linkedin.catalog import sync_slices
from src.jobstore import heartbeat
from src.sources import service
from src.sources.base import JsonClient
from src.sources.freehire import build_slices
from src.sources.provenance import format_report, freehire_provenance, italian_sample
from tests.test_sources_freehire import ATS_HIT, AGGREGATOR_HIT, Pages, _body, _cfg, _hits

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def _client(cfg, pages):
    return JsonClient(cfg, get=pages, sleep=lambda _s: None, clock=lambda: 0.0)


def test_a_due_slice_is_walked_and_scheduled_for_tomorrow(store_conn):
    cfg = _cfg(page_size=5)
    sync_slices(store_conn, build_slices(cfg), NOW, "freehire")
    store_conn.commit()
    pages = Pages(_body(_hits(0, 3)))
    tick = service.collect_source_once(store_conn, "freehire", cfg, _client(cfg, pages), now_fn=lambda: NOW)
    assert tick.did_work and tick.detail["new_jobs"] == 3
    with store_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM slices WHERE due_at > %s", (NOW,))
        assert cur.fetchone()[0] == 1


def test_an_api_failure_marks_the_slice_for_a_retry_and_records_nothing(store_conn):
    cfg = _cfg(page_size=5)
    sync_slices(store_conn, build_slices(cfg), NOW, "freehire")
    store_conn.commit()
    body = {**_body(_hits(0, 5)), "meta": {"ignored_params": [{"param": "category"}]}}
    tick = service.collect_source_once(store_conn, "freehire", cfg, _client(cfg, Pages(body)), now_fn=lambda: NOW)
    assert tick.detail["stop_reason"] == "error"
    with store_conn.cursor() as cur:
        cur.execute("SELECT stop_reason FROM slices WHERE stop_reason IS NOT NULL")
        assert cur.fetchall() == [("error",)]
        cur.execute("SELECT count(*) FROM jobs")
        assert cur.fetchone()[0] == 0


def test_nothing_due_means_no_work(store_conn):
    cfg = _cfg()
    tick = service.collect_source_once(store_conn, "freehire", cfg, _client(cfg, Pages()), now_fn=lambda: NOW)
    assert not tick.did_work


def _clock(start, step_seconds=20):
    state = {"now": start}

    def now_fn():
        current = state["now"]
        state["now"] = current + timedelta(seconds=step_seconds)
        return current
    return now_fn


def test_a_daily_timer_walks_every_slice_on_both_days(store_conn):
    # The timer fires at the same clock time each day; a walk that finishes a few seconds
    # after 03:00 must still be due at the next day's 03:00, or every other day is skipped.
    cfg = _cfg(page_size=5)
    sync_slices(store_conn, build_slices(cfg), NOW, "freehire")
    store_conn.commit()
    first = service.collect_due_slices(
        store_conn, "freehire", cfg,
        _client(cfg, Pages(*[_body(_hits(index, 1)) for index in range(8)])), now_fn=_clock(NOW))
    assert len(first) == 8
    second = service.collect_due_slices(
        store_conn, "freehire", cfg,
        _client(cfg, Pages(*[_body(_hits(index, 1)) for index in range(8)])),
        now_fn=_clock(NOW + timedelta(days=1)))
    assert len(second) == 8


def test_oneshot_mode_drains_every_due_slice(store_conn):
    cfg = _cfg(page_size=5)
    sync_slices(store_conn, build_slices(cfg), NOW, "freehire")
    store_conn.commit()
    pages = Pages(*[_body(_hits(index, 1)) for index in range(8)])
    ticks = service.collect_due_slices(store_conn, "freehire", cfg, _client(cfg, pages), now_fn=lambda: NOW)
    assert len(ticks) == 8 and all(t.did_work for t in ticks)
    with store_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM slices WHERE due_at <= %s", (NOW,))
        assert cur.fetchone()[0] == 0


def test_a_drain_records_a_heartbeat_for_the_collector(store_conn):
    cfg = _cfg(page_size=5)
    sync_slices(store_conn, build_slices(cfg), NOW, "freehire")
    store_conn.commit()
    pages = Pages(*[_body(_hits(index, 1)) for index in range(8)])
    service.collect_due_slices(store_conn, "freehire", cfg, _client(cfg, pages), now_fn=lambda: NOW)
    beats = {beat.service: beat for beat in heartbeat.read_all(store_conn)}
    assert beats["collect-freehire"].state == "ok"
    assert beats["collect-freehire"].detail["slices"] == 8


def test_a_drain_that_only_errors_beats_error(store_conn):
    cfg = _cfg(page_size=5)
    sync_slices(store_conn, build_slices(cfg), NOW, "freehire")
    store_conn.commit()
    body = {**_body(_hits(0, 5)), "meta": {"ignored_params": [{"param": "category"}]}}
    service.collect_due_slices(store_conn, "freehire", cfg, _client(cfg, Pages(*[body] * 8)),
                               now_fn=lambda: NOW)
    beats = {beat.service: beat for beat in heartbeat.read_all(store_conn)}
    assert beats["collect-freehire"].state == "error"


def test_a_oneshot_run_of_only_errors_exits_nonzero(store_conn, store_conn_factory, monkeypatch):
    cfg = _cfg(page_size=5)
    monkeypatch.setattr(service, "load_source_config", lambda name: cfg)
    monkeypatch.setattr(service, "store_url", lambda: "unused")
    monkeypatch.setattr(service, "connect", lambda url: store_conn_factory(store_conn))
    body = {**_body(_hits(0, 5)), "meta": {"ignored_params": [{"param": "category"}]}}
    assert service.main(["freehire", "--once"], get=Pages(*[body] * 8)) == 1


def test_a_one_hour_interval_drains_each_slice_once(store_conn):
    # A hand-built config can still reach collect_due_slices; a 1 h interval must not make a
    # finished slice due again inside the same run (load_source_config rejects it from the file).
    cfg = replace(_cfg(page_size=5), interval_hours=1)
    sync_slices(store_conn, build_slices(cfg), NOW, "freehire")
    store_conn.commit()
    pages = Pages(*[_body(_hits(index, 1)) for index in range(8)])
    ticks = service.collect_due_slices(store_conn, "freehire", cfg, _client(cfg, pages),
                                       now_fn=lambda: NOW)
    assert len(ticks) == 8


def test_a_crash_during_a_drain_leaves_an_error_beat(store_conn, monkeypatch):
    cfg = _cfg(page_size=5)
    sync_slices(store_conn, build_slices(cfg), NOW, "freehire")
    store_conn.commit()

    def crash(*_args, **_kwargs):
        raise RuntimeError("store write failed")

    # A malformed hit is skipped, not a crash, so break a step a walk cannot guard against.
    monkeypatch.setattr("src.sources.freehire.record_cards", crash)
    with pytest.raises(RuntimeError):
        service.collect_due_slices(store_conn, "freehire", cfg, _client(cfg, Pages(_body(_hits(0, 1)))),
                                   now_fn=lambda: NOW)
    beats = {beat.service: beat for beat in heartbeat.read_all(store_conn)}
    assert beats["collect-freehire"].state == "error"
    assert beats["collect-freehire"].detail == {"crashed": "RuntimeError"}


def test_a_disabled_source_refuses_to_run(tmp_path, monkeypatch, capsys):
    path = tmp_path / "sources.json"
    path.write_text(json.dumps({"freehire": {"enabled": False, "base_url": "x", "user_agent": "u",
                                             "spacing_seconds": [1, 2], "interval_hours": 24,
                                             "throttle": {"retries_per_page": None,
                                                          "default_retry_after_seconds": 60,
                                                          "max_wait_seconds": 300,
                                                          "run_budget_seconds": 1700}}}))
    monkeypatch.setattr(service, "load_source_config",
                        lambda name: __import__("src.sources.base", fromlist=["x"]).load_source_config(name, str(path)))
    assert service.main(["freehire", "--once"]) == 0
    assert "disabled in config/sources.json" in capsys.readouterr().out


def test_provenance_groups_hits_by_origin_and_flags_aggregators(store_conn):
    from src.jobstore.sightings import record_sighting
    from src.sources.freehire import item_to_card
    for n in range(6):
        record_sighting(store_conn, item_to_card({**AGGREGATOR_HIT, "public_slug": f"agg-{n}",
                                                  "title": f"Role {n}", "source": "jobgether"}, None, 1),
                        datetime.now(timezone.utc))
    for n in range(6):
        record_sighting(store_conn, item_to_card({**ATS_HIT, "public_slug": f"ats-{n}", "title": f"Role {n}",
                                                  "countries": ["it"]}, None, 1), datetime.now(timezone.utc))
    store_conn.commit()
    rows = {row["origin"]: row for row in freehire_provenance(store_conn, 7)}
    assert rows["jobgether"]["hits"] == 6 and rows["jobgether"]["employer_link"] == 0 and rows["jobgether"]["suspect"]
    assert rows["greenhouse"]["employer_link"] == 6 and not rows["greenhouse"]["suspect"]
    report = format_report(list(rows.values()), italian_sample(store_conn, 3))
    assert "jobgether: 6, 0, 0, 0   <-- looks like an aggregator" in report
    assert "Italian hits to read by hand:" in report and "greenhouse" in report


def test_a_resume_run_walks_only_unfinished_slices(store_conn):
    cfg = _cfg(page_size=5)
    sync_slices(store_conn, build_slices(cfg), NOW, "freehire")
    with store_conn.cursor() as cur:
        cur.execute("UPDATE slices SET stop_reason = 'known_page'")
        cur.execute("UPDATE slices SET stop_reason = 'rate_limited' WHERE id = "
                    "(SELECT id FROM slices ORDER BY id LIMIT 1)")
    store_conn.commit()
    pages = Pages(_body(_hits(0, 1)))
    ticks = service.collect_due_slices(store_conn, "freehire", cfg, _client(cfg, pages),
                                       now_fn=lambda: NOW, unfinished_only=True)
    assert len(ticks) == 1 and ticks[0].did_work
    with store_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM slices WHERE stop_reason = 'known_page'")
        assert cur.fetchone()[0] == 7


def test_a_resume_run_with_nothing_to_resume_leaves_the_heartbeat_alone(store_conn):
    cfg = _cfg(page_size=5)
    sync_slices(store_conn, build_slices(cfg), NOW, "freehire")
    with store_conn.cursor() as cur:
        cur.execute("UPDATE slices SET stop_reason = 'known_page'")
    heartbeat.start(store_conn, "collect-freehire", now=NOW)
    heartbeat.beat(store_conn, "collect-freehire", "ok", {"slices": 8}, now=NOW)
    store_conn.commit()
    ticks = service.collect_due_slices(store_conn, "freehire", cfg, _client(cfg, Pages()),
                                       now_fn=lambda: NOW, unfinished_only=True)
    assert ticks == []
    beats = {beat.service: beat for beat in heartbeat.read_all(store_conn)}
    assert beats["collect-freehire"].state == "ok" and beats["collect-freehire"].detail == {"slices": 8}


def test_a_second_run_of_the_same_source_exits_without_a_request(store_conn, store_conn_factory):
    from src.services.runner import acquire_single_instance
    cfg = _cfg(page_size=5)
    sync_slices(store_conn, build_slices(cfg), NOW, "freehire")
    store_conn.commit()
    holder = store_conn_factory(store_conn)
    holder.autocommit = True
    assert acquire_single_instance(holder, "collect-freehire")
    get = Pages(_body(_hits(0, 1)))
    assert service.run_oneshot(store_conn, "freehire", cfg, _client(cfg, get), resume=False,
                               now_fn=lambda: NOW) == 0
    assert get.calls == []
    holder.close()


def test_a_walk_finished_by_a_resume_run_is_due_at_the_next_daily_run(store_conn):
    from src.linkedin.walk import claim_due_slice
    cfg = _cfg(page_size=5)
    sync_slices(store_conn, build_slices(cfg), NOW, "freehire")
    with store_conn.cursor() as cur:
        cur.execute("UPDATE slices SET stop_reason = 'known_page', due_at = %s",
                    (NOW + timedelta(days=30),))
        cur.execute("UPDATE slices SET stop_reason = 'rate_limited', due_at = %s WHERE id = "
                    "(SELECT id FROM slices ORDER BY id LIMIT 1)", (NOW,))
        cur.execute("SELECT id FROM slices ORDER BY id LIMIT 1")
        resumed_id = cur.fetchone()[0]
    store_conn.commit()
    resume_at = NOW + timedelta(hours=2)
    pages = Pages(_body(_hits(0, 1)))
    assert len(service.collect_due_slices(store_conn, "freehire", cfg, _client(cfg, pages),
                                          now_fn=lambda: resume_at, unfinished_only=True)) == 1
    # Only the next daily run can take the now-finished slice, so it must be due by then.
    row = claim_due_slice(store_conn, NOW + timedelta(days=1), "freehire")
    assert row is not None and row.id == resumed_id


def test_a_resume_run_that_recovers_a_walk_re_beats_ok(store_conn):
    cfg = _cfg(page_size=5)
    sync_slices(store_conn, build_slices(cfg), NOW, "freehire")
    with store_conn.cursor() as cur:
        cur.execute("UPDATE slices SET stop_reason = 'known_page'")
        cur.execute("UPDATE slices SET stop_reason = 'error', due_at = %s WHERE id = "
                    "(SELECT id FROM slices ORDER BY id LIMIT 1)", (NOW,))
    heartbeat.start(store_conn, "collect-freehire", now=NOW)
    heartbeat.beat(store_conn, "collect-freehire", "error", {"slices": 8}, now=NOW)
    store_conn.commit()
    pages = Pages(_body(_hits(0, 1)))
    service.collect_due_slices(store_conn, "freehire", cfg, _client(cfg, pages),
                               now_fn=lambda: NOW, unfinished_only=True)
    beats = {beat.service: beat for beat in heartbeat.read_all(store_conn)}
    assert beats["collect-freehire"].state == "ok"
