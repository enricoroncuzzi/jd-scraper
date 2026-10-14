from datetime import date, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import telegram
from src.delivery.daily import deliver_tier
from src.delivery.messages import render_day_summary, render_tier_report, tier_digest_message
from src.delivery.settings import DeliverySettings
from src.jobstore import budget, heartbeat
from src.processor.config import load_processor_config
from tests.delivery_helpers import NOW, scored_job

DAY = date(2026, 10, 14)


@pytest.fixture(scope="module")
def cfg():
    return load_processor_config()


@pytest.fixture
def settings(tmp_path):
    return DeliverySettings(output_path=str(tmp_path), delivery_time=time(8, 0), timezone=ZoneInfo("Europe/Rome"))


@pytest.fixture(autouse=True)
def _freeze_telegram_date(monkeypatch):
    class Frozen(date):
        @classmethod
        def today(cls):
            return DAY
    monkeypatch.setattr(telegram, "date", Frozen)


def test_the_tier_digest_message_is_the_legacy_formatter_output(store_conn, cfg, settings):
    scored_job(store_conn, 1, 9)
    delivery = deliver_tier(store_conn, cfg, settings, 1, DAY, NOW)
    text = tier_digest_message(delivery, cfg.policies[1])
    assert text.startswith("Hey Enrico! Tier 1 (Italy, full remote) digest is ready.\n\nJob Digest - 2026-10-14")
    assert "*Confirmed full-remote (1):*" in text and "AI Engineer 1 - Acme 1" in text


def test_an_empty_tier_message_says_nothing_new(store_conn, cfg, settings):
    delivery = deliver_tier(store_conn, cfg, settings, 3, DAY, NOW)
    assert tier_digest_message(delivery, cfg.policies[3]).endswith("No new offers after dedup filter.")


def test_the_tier_report_summarises_the_last_24_hours(store_conn, cfg, settings):
    scored_job(store_conn, 1, 9)
    scored_job(store_conn, 2, 5)
    scored_job(store_conn, 3, 1, stage="filtered_out", verdict="rejected")
    scored_job(store_conn, 4, None, stage="described")
    with store_conn.cursor() as cur:
        cur.execute("INSERT INTO slices (source, role, area, tier, base_interval_seconds, interval_seconds, "
                    "last_walked_at, stop_reason) VALUES ('linkedin', 'AI Engineer', 'Italy', 1, 1, 1, %s, "
                    "'end_of_results'), ('linkedin', 'ML Engineer', 'Italy', 1, 1, 1, NULL, NULL)", (NOW,))
    delivery = deliver_tier(store_conn, cfg, settings, 1, DAY, NOW)
    report = render_tier_report(store_conn, 1, cfg.policies[1], delivery, NOW)
    assert "Tier 1 (Italy): last 24 hours of collection" in report
    assert "Slices walked: 1/2 (end_of_results 1)" in report
    assert "New jobs found: linkedin 4" in report
    assert "Delivered today: 2 (8+: 1)" in report
    assert "Dropped: remote_rejected 1" in report
    assert "Waiting: 0 for a description, 1 for a score" in report


def test_the_day_summary_folds_in_the_per_tier_collection_reports(store_conn, cfg, settings):
    scored_job(store_conn, 1, 9)
    deliveries = {t: deliver_tier(store_conn, cfg, settings, t, DAY, NOW) for t in (1, 2, 3, 4)}
    budget.add_usage(store_conn, "openrouter", requests=812, now=NOW)
    budget.add_usage(store_conn, "groq", requests=3, input_tokens=60000, output_tokens=5000, now=NOW)
    heartbeat.start(store_conn, "process", now=NOW)
    heartbeat.beat(store_conn, "process", "running", now=NOW)
    store_conn.commit()
    (message,) = render_day_summary(store_conn, cfg, settings, deliveries, NOW, DAY)
    assert "Day summary - 2026-10-14" in message
    assert "Tier 1: found 1, delivered 1, 8+ 1, packaged 0" in message
    # The four per-tier collection reports live inside the day summary, not as
    # separate messages (the captain's Telegram volume decision).
    assert "Tier 1 (Italy): last 24 hours of collection" in message
    assert "Tier 4 (UK): last 24 hours of collection" in message
    assert "OpenRouter 812/1000 requests - HIGH" in message
    assert "Groq 65,000/200,000 tokens" in message
    assert "process: running" in message and "deliver: never reported" in message
    assert "linkedin: 1 new jobs" in message


def test_the_day_summary_lists_a_timer_collector_and_its_error(store_conn, cfg, settings, tmp_path, monkeypatch):
    from src.delivery import health
    sources = tmp_path / "sources.json"
    sources.write_text('{"freehire": {"enabled": true, "interval_hours": 24}}')
    monkeypatch.setattr(health, "SOURCES_CONFIG_PATH", str(sources))
    heartbeat.start(store_conn, "collect-freehire", now=NOW)
    heartbeat.beat(store_conn, "collect-freehire", "error", {"slices": 8}, now=NOW)
    store_conn.commit()
    deliveries = {t: deliver_tier(store_conn, cfg, settings, t, DAY, NOW) for t in (1, 2, 3, 4)}
    (message,) = render_day_summary(store_conn, cfg, settings, deliveries, NOW, DAY)
    assert "collect-freehire: error" in message
    # A beat inside the collector's interval is not DOWN, even though it is over an hour old.
    heartbeat.beat(store_conn, "collect-freehire", "ok", now=NOW - timedelta(hours=20))
    store_conn.commit()
    (message,) = render_day_summary(store_conn, cfg, settings, deliveries, NOW, DAY)
    assert "collect-freehire: ok" in message and "collect-freehire: DOWN" not in message


def test_long_summaries_are_split_for_telegram(store_conn, cfg, settings):
    deliveries = {t: deliver_tier(store_conn, cfg, settings, t, DAY, NOW) for t in (1, 2, 3, 4)}
    messages = render_day_summary(store_conn, cfg, settings, deliveries, NOW, DAY)
    assert all(len(m) <= 4096 for m in messages)
