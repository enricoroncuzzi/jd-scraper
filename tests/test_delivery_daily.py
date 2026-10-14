import os
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.delivery.daily import (
    deliver_tier, delivered_jobs, delivery_pending, mark_telegram_sent, unsent_telegram,
)
from src.delivery.settings import DeliverySettings, load_settings
from src.processor.config import load_processor_config
from tests.delivery_helpers import NOW, scored_job

DAY = date(2026, 10, 14)


@pytest.fixture(scope="module")
def cfg():
    return load_processor_config()


@pytest.fixture
def settings(tmp_path):
    return DeliverySettings(output_path=str(tmp_path), delivery_time=time(8, 0),
                            timezone=ZoneInfo("Europe/Rome"))


def _read(settings, tier, *parts):
    with open(os.path.join(settings.output_path, DAY.isoformat(), f"tier{tier}", *parts), encoding="utf-8") as f:
        return f.read()


def _job(conn, job_id):
    with conn.cursor() as cur:
        cur.execute("SELECT stage, delivered_at, delivery_day, note_day, archived_at FROM jobs WHERE id = %s", (job_id,))
        return cur.fetchone()


def test_the_shipped_settings_load():
    s = load_settings()
    assert (s.delivery_time, str(s.timezone)) == (time(8, 0), "Europe/Rome")
    assert s.expected_services == ("collect-linkedin", "process", "deliver")


def test_delivery_starts_at_the_local_time_not_utc(settings):
    assert settings.delivery_started(datetime(2026, 10, 14, 5, 59, tzinfo=timezone.utc)) is False  # 07:59 Rome
    assert settings.delivery_started(datetime(2026, 10, 14, 6, 0, tzinfo=timezone.utc)) is True   # 08:00 Rome
    assert settings.local_day(datetime(2026, 10, 14, 22, 30, tzinfo=timezone.utc)) == date(2026, 10, 15)


def test_a_tier_is_written_in_todays_layout_and_marked_delivered(store_conn, cfg, settings):
    high = scored_job(store_conn, 1, 9)
    low = scored_job(store_conn, 2, 4)
    result = deliver_tier(store_conn, cfg, settings, 1, DAY, NOW)
    assert [j.id for j in result.jobs] == [high, low]
    digest = _read(settings, 1, "digest.md")
    assert "# Job Digest - 2026-10-14" in digest and "AI Engineer 1" in digest
    assert "1 offers below threshold" in digest
    assert len(os.listdir(os.path.join(settings.output_path, "2026-10-14", "tier1", "scraped"))) == 2
    assert "None. Every offer was confirmed" in _read(settings, 1, "rejected.md")
    assert _job(store_conn, high)[:4] == ("delivered", NOW, DAY, DAY)
    assert _job(store_conn, high)[4] is None


def test_tier_two_writes_no_rejected_file(store_conn, cfg, settings):
    scored_job(store_conn, 1, 9, tier=2, verdict="not_checked", location="Zurich, Switzerland")
    deliver_tier(store_conn, cfg, settings, 2, DAY, NOW)
    assert not os.path.exists(os.path.join(settings.output_path, "2026-10-14", "tier2", "rejected.md"))


def test_an_empty_tier_still_gets_a_digest(store_conn, cfg, settings):
    deliver_tier(store_conn, cfg, settings, 3, DAY, NOW)
    assert "No new offers after dedup filter." in _read(settings, 3, "digest.md")


def test_a_restart_after_delivery_does_not_overwrite_the_digest_with_an_empty_one(store_conn, cfg, settings):
    scored_job(store_conn, 1, 9)
    deliver_tier(store_conn, cfg, settings, 1, DAY, NOW)
    good = _read(settings, 1, "digest.md")
    assert deliver_tier(store_conn, cfg, settings, 1, DAY, NOW + timedelta(hours=2)) is None
    assert _read(settings, 1, "digest.md") == good


def test_jobs_scored_after_the_delivery_wait_for_the_next_day(store_conn, cfg, settings):
    deliver_tier(store_conn, cfg, settings, 1, DAY, NOW)
    late = scored_job(store_conn, 5, 9, when=NOW + timedelta(hours=3))
    assert _job(store_conn, late)[0] == "scored"
    next_day = date(2026, 10, 15)
    result = deliver_tier(store_conn, cfg, settings, 1, next_day, NOW + timedelta(days=1))
    assert [j.id for j in result.jobs] == [late]


def test_rejected_jobs_are_listed_once_in_rejected_md(store_conn, cfg, settings):
    scored_job(store_conn, 1, 1, verdict="rejected", stage="filtered_out", reason="Needs office days.")
    deliver_tier(store_conn, cfg, settings, 1, DAY, NOW)
    assert "Needs office days." in _read(settings, 1, "rejected.md")
    again = deliver_tier(store_conn, cfg, settings, 1, date(2026, 10, 15), NOW + timedelta(days=1))
    assert again.rejected == []


def test_the_waiting_count_feeds_the_digest_footer(store_conn, cfg, settings):
    scored_job(store_conn, 1, 9)
    scored_job(store_conn, 2, None, stage="described")
    deliver_tier(store_conn, cfg, settings, 1, DAY, NOW)
    assert "1 offer(s) still unscored" in _read(settings, 1, "digest.md")


def test_an_alerted_job_keeps_its_earlier_note_and_is_marked_sent_earlier(store_conn, cfg, settings):
    yesterday = date(2026, 10, 13)
    job = scored_job(store_conn, 1, 9, alerted_at=NOW - timedelta(days=1), note_day=yesterday)
    deliver_tier(store_conn, cfg, settings, 1, DAY, NOW)
    scraped = os.path.join(settings.output_path, "2026-10-14", "tier1", "scraped")
    assert not os.path.exists(scraped) or os.listdir(scraped) == []
    digest = _read(settings, 1, "digest.md")
    assert "sent earlier" in digest and "../../2026-10-13/tier1/scraped/" in digest
    assert _job(store_conn, job)[3] == yesterday  # the note stays where the alert put it


def test_delivery_is_pending_from_the_delivery_time_until_every_tier_is_done(store_conn, cfg, settings):
    early = datetime(2026, 10, 14, 5, 0, tzinfo=timezone.utc)
    assert delivery_pending(store_conn, settings, [1, 2, 3, 4], early) is None
    assert delivery_pending(store_conn, settings, [1, 2, 3, 4], NOW) == DAY
    for tier in (1, 2, 3, 4):
        deliver_tier(store_conn, cfg, settings, tier, DAY, NOW)
    assert delivery_pending(store_conn, settings, [1, 2, 3, 4], NOW) is None


def test_the_telegram_message_is_retried_until_marked_sent(store_conn, cfg, settings):
    job = scored_job(store_conn, 1, 9)
    deliver_tier(store_conn, cfg, settings, 1, DAY, NOW)
    assert unsent_telegram(store_conn, DAY) == [1]
    assert [j.id for j in delivered_jobs(store_conn, DAY, 1)] == [job]
    mark_telegram_sent(store_conn, DAY, 1, NOW)
    assert unsent_telegram(store_conn, DAY) == []
