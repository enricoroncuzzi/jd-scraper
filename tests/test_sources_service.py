import json
from datetime import datetime, timezone


from src.linkedin.catalog import sync_slices
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


def test_a_disabled_source_refuses_to_run(tmp_path, monkeypatch, capsys):
    path = tmp_path / "sources.json"
    path.write_text(json.dumps({"freehire": {"enabled": False, "base_url": "x", "user_agent": "u",
                                             "min_interval_seconds": 1, "interval_hours": 24}}))
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
