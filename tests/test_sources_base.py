from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import json
import pytest
import requests

from src.jobstore.sightings import CardInput
from src.linkedin.catalog import SliceSpec, sync_slices
from src.linkedin.walk import claim_due_slice
from src.sources.base import (
    JsonClient, SourceConfig, SourceError, SourceThrottled, SourceWalk, finish_source_walk,
    load_source_config, record_cards,
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


def _client(get, sleeps=None, min_interval=1.0):
    cfg = SourceConfig(True, "https://api.example/v1", "owner/project/1.0 (+https://x)", min_interval, 24, {})
    clock = {"t": 0.0}
    sleeps = sleeps if sleeps is not None else []

    def sleep(seconds):
        sleeps.append(seconds)
        clock["t"] += seconds
    return JsonClient(cfg, get=get, sleep=sleep, clock=lambda: clock["t"]), sleeps


def test_a_too_short_interval_is_rejected(tmp_path):
    path = tmp_path / "sources.json"
    path.write_text(json.dumps({"freehire": {"enabled": True, "base_url": "x", "user_agent": "u",
                                             "min_interval_seconds": 1, "interval_hours": 1}}))
    with pytest.raises(ValueError, match="interval_hours"):
        load_source_config("freehire", str(path))


def test_the_shipped_config_enables_sources_one_at_a_time_and_identifies_the_project():
    enabled = {"freehire": True, "himalayas": False}  # the captain approves each source in turn
    for name, expected in enabled.items():
        cfg = load_source_config(name)
        assert cfg.enabled is expected and "jd-scraper" in cfg.user_agent


def test_a_request_identifies_the_project_and_keeps_the_minimum_gap():
    get = Scripted(_response(body={"a": 1}), _response(body={"b": 2}))
    client, sleeps = _client(get)
    assert client.get_json("/jobs", {"q": "x"}) == {"a": 1}
    assert client.get_json("/jobs", {"q": "y"}) == {"b": 2}
    assert get.calls[0][0] == "https://api.example/v1/jobs"
    assert get.calls[0][2]["User-Agent"].startswith("owner/project")
    assert sleeps == [1.0]


def test_a_nearly_empty_rate_limit_budget_pauses_until_it_resets():
    get = Scripted(_response(body={}, headers={"X-RateLimit-Remaining": "1", "X-RateLimit-Reset": "7"}))
    client, sleeps = _client(get)
    client.get_json("/jobs", {})
    assert sleeps == [8]


def test_429_waits_for_retry_after_then_succeeds():
    get = Scripted(_response(429, headers={"Retry-After": "12"}), _response(body={"ok": True}))
    client, sleeps = _client(get)
    assert client.get_json("/jobs", {}) == {"ok": True}
    assert 12 in sleeps


def test_429_that_never_clears_raises_throttled():
    get = Scripted(*[_response(429) for _ in range(4)])
    client, _ = _client(get)
    with pytest.raises(SourceThrottled):
        client.get_json("/jobs", {})


def test_5xx_and_network_errors_are_retried_then_fail():
    get = Scripted(_response(503), requests.ConnectionError("down"), _response(body={"ok": 1}))
    client, sleeps = _client(get)
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
    client, sleeps = _client(Scripted(bad, _response(body={"ok": True})))
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
