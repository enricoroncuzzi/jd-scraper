from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import json
import pytest
import requests

from src.jobstore.sightings import CardInput
from src.linkedin.catalog import SliceSpec, sync_slices
from src.linkedin.walk import claim_due_slice
from src.sources.base import (
    JsonClient, SourceConfig, SourceError, SourceThrottled, SourceWalk, ThrottlePolicy,
    finish_source_walk, load_source_config, record_cards,
)

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def _response(status=200, body=None, headers=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body if body is not None else {}
    resp.headers = headers or {}
    return resp


class Scripted:
    def __init__(self, *items):
        self.items, self.calls = list(items), []

    def __call__(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _client(get, sleeps=None, spacing=(1.0, 2.0), retries=None, uniform=lambda a, b: a):
    """Returns (client, sleeps, clock): `clock` is the fake monotonic dict {"t": 0.0};
    sleeps advance it."""
    cfg = SourceConfig(True, "https://api.example/v1", "owner/project/1.0 (+https://x)", spacing, 24,
                       ThrottlePolicy(retries, 60.0, 300.0, 1700.0), {})
    clock = {"t": 0.0}
    sleeps = sleeps if sleeps is not None else []

    def sleep(seconds):
        sleeps.append(seconds)
        clock["t"] += seconds
    return JsonClient(cfg, get=get, sleep=sleep, clock=lambda: clock["t"], uniform=uniform), sleeps, clock


def test_a_too_short_interval_is_rejected(tmp_path):
    path = tmp_path / "sources.json"
    path.write_text(json.dumps({"freehire": {
        "enabled": True, "base_url": "x", "user_agent": "u", "spacing_seconds": [1, 2],
        "interval_hours": 1, "throttle": {"retries_per_page": None, "default_retry_after_seconds": 60,
                                          "max_wait_seconds": 300, "run_budget_seconds": 1700}}}))
    with pytest.raises(ValueError, match="interval_hours"):
        load_source_config("freehire", str(path))


def test_the_shipped_config_enables_sources_one_at_a_time_and_identifies_the_project():
    enabled = {"freehire": True, "himalayas": True}  # the captain approves each source in turn
    for name, expected in enabled.items():
        cfg = load_source_config(name)
        assert cfg.enabled is expected and "jd-scraper" in cfg.user_agent


def test_a_request_identifies_the_project_and_keeps_the_minimum_gap():
    get = Scripted(_response(body={"a": 1}), _response(body={"b": 2}))
    client, sleeps, _clock = _client(get)
    assert client.get_json("/jobs", {"q": "x"}) == {"a": 1}
    assert client.get_json("/jobs", {"q": "y"}) == {"b": 2}
    assert get.calls[0][0] == "https://api.example/v1/jobs"
    assert get.calls[0][2]["User-Agent"].startswith("owner/project")
    assert sleeps == [1.0]


def test_requests_are_spaced_by_a_jittered_gap():
    get = Scripted(_response(body={}), _response(body={}))
    client, sleeps, _ = _client(get, uniform=lambda a, b: b)
    client.get_json("/jobs", {})
    assert sleeps == []  # the first request never waits
    client.get_json("/jobs", {})
    assert sleeps == [2.0]
    client, sleeps, _ = _client(Scripted(_response(body={}), _response(body={})), uniform=lambda a, b: a)
    client.get_json("/jobs", {})
    client.get_json("/jobs", {})
    assert sleeps == [1.0]


def test_a_nearly_empty_rate_limit_budget_pauses_until_it_resets():
    get = Scripted(_response(body={}, headers={"X-RateLimit-Remaining": "1", "X-RateLimit-Reset": "7"}))
    client, sleeps, _clock = _client(get)
    client.get_json("/jobs", {})
    assert sleeps == [8]


def test_a_429_waits_exactly_retry_after_then_continues():
    get = Scripted(_response(429, headers={"Retry-After": "7"}), _response(body={"ok": 1}))
    client, sleeps, _clock = _client(get)
    assert client.get_json("/jobs", {}) == {"ok": 1} and 7 in sleeps


def test_a_retry_after_of_zero_does_not_wait():
    get = Scripted(_response(429, headers={"Retry-After": "0"}), _response(body={"ok": 1}))
    client, sleeps, _clock = _client(get)
    assert client.get_json("/jobs", {}) == {"ok": 1} and 60.0 not in sleeps


def test_freehire_keeps_waiting_through_repeated_429s_within_its_bounds():
    get = Scripted(*[_response(429, headers={"Retry-After": "10"})] * 5, _response(body={"ok": 1}))
    assert _client(get)[0].get_json("/jobs", {}) == {"ok": 1}


def test_a_single_wait_over_five_minutes_leaves_the_walk_unfinished():
    client, sleeps, _clock = _client(Scripted(_response(429, headers={"Retry-After": "301"})))
    with pytest.raises(SourceThrottled):
        client.get_json("/jobs", {})
    assert 301 not in sleeps


def test_waits_that_would_outlive_the_run_give_up():
    client, _sleeps, clock = _client(Scripted(_response(429, headers={"Retry-After": "60"})))
    clock["t"] = 1650.0  # the run started at 0; 1650 + 60 > 1700
    with pytest.raises(SourceThrottled):
        client.get_json("/jobs", {})


def test_himalayas_retries_a_429_once_then_gives_up():
    client, sleeps, _clock = _client(Scripted(_response(429), _response(429)), retries=1)
    with pytest.raises(SourceThrottled):
        client.get_json("/jobs", {})
    assert sleeps.count(60.0) == 1


def test_a_client_uses_one_session_for_all_its_requests(monkeypatch):
    from types import SimpleNamespace
    made = []

    def session():
        made.append(1)
        return SimpleNamespace(get=lambda *args, **kwargs: _response(body={}))
    monkeypatch.setattr("src.sources.base.new_session", session)
    cfg = SourceConfig(True, "https://api.example/v1", "owner/project/1.0", (1.0, 2.0), 24,
                       ThrottlePolicy(None, 60.0, 300.0, 1700.0), {})
    client = JsonClient(cfg, sleep=lambda _s: None, clock=lambda: 0.0, uniform=lambda a, b: a)
    client.get_json("/jobs", {})
    client.get_json("/jobs", {})
    assert len(made) == 1


def test_the_shipped_sources_use_the_designed_spacing_and_page_size():
    fh, hi = load_source_config("freehire"), load_source_config("himalayas")
    assert fh.spacing_seconds == (1.0, 2.0) and fh.options["page_size"] == 100
    assert fh.throttle.retries_per_page is None and fh.throttle.max_wait_seconds == 300
    assert hi.spacing_seconds == (2.0, 5.0) and hi.options["max_pages"] == 15
    assert hi.throttle.retries_per_page == 1 and hi.throttle.default_retry_after_seconds == 60


def test_5xx_and_network_errors_are_retried_then_fail():
    get = Scripted(_response(503), requests.ConnectionError("down"), _response(body={"ok": 1}))
    client, sleeps, _clock = _client(get)
    assert client.get_json("/jobs", {}) == {"ok": 1}
    get = Scripted(*[_response(500) for _ in range(4)])
    with pytest.raises(SourceError):
        _client(get)[0].get_json("/jobs", {})


def test_a_4xx_fails_at_once():
    get = Scripted(_response(404))
    with pytest.raises(SourceError, match="404"):
        _client(get)[0].get_json("/jobs", {})
    assert len(get.calls) == 1


def test_a_non_json_200_is_retried_like_a_server_error():
    bad = _response(200)
    bad.json.side_effect = ValueError("not json")
    client, sleeps, _clock = _client(Scripted(bad, _response(body={"ok": True})))
    assert client.get_json("/jobs", {}) == {"ok": True}
    assert 5 in sleeps


def test_a_200_that_is_never_json_raises_source_error():
    bad = []
    for _ in range(4):
        resp = _response(200)
        resp.json.side_effect = ValueError("not json")
        bad.append(resp)
    with pytest.raises(SourceError, match="invalid JSON"):
        _client(Scripted(*bad))[0].get_json("/jobs", {})


def test_record_cards_counts_new_jobs_and_flags_a_fully_known_page(store_conn):
    cards = [CardInput(source="freehire", source_job_id=f"j{n}", source_url=f"https://freehire.me/{n}",
                       title=f"t{n}", company="c") for n in range(3)]
    assert record_cards(store_conn, cards, NOW) == (3, False)
    assert record_cards(store_conn, cards, NOW) == (0, True)
    assert record_cards(store_conn, [], NOW) == (0, False)


def test_finishing_a_daily_walk_schedules_the_next_day_and_a_failed_one_30_minutes_later(store_conn):
    sync_slices(store_conn, [SliceSpec("freehire", "ai_engineering", "countries=it", 1, 86400)], NOW, "freehire")
    store_conn.commit()
    row = claim_due_slice(store_conn, NOW, "freehire")
    finish_source_walk(store_conn, row, SourceWalk(3, 120, 40, "known_page"), NOW, NOW, 86400)
    with store_conn.cursor() as cur:
        cur.execute("SELECT last_walked_at, stop_reason, due_at, resume_start FROM slices")
        assert cur.fetchone() == (NOW, "known_page", NOW + timedelta(days=1), 0)
    row = claim_due_slice(store_conn, NOW + timedelta(days=1), "freehire")
    finish_source_walk(store_conn, row, SourceWalk(2, 60, 5, "rate_limited", next_offset=100),
                       NOW + timedelta(days=1), NOW + timedelta(days=1), 86400)
    with store_conn.cursor() as cur:
        cur.execute("SELECT last_walked_at, stop_reason, due_at, resume_start FROM slices")
        assert cur.fetchone() == (NOW, "rate_limited", NOW + timedelta(days=1, minutes=30), 100)
