"""Sightings in, delivered folder out: the three services against one local Postgres,
with LinkedIn, the language models, Telegram and Neon replaced by fakes."""
import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg2
import pytest

from src import telegram
from src.delivery.archive import init_archive
from src.delivery.service import DeliverService
from src.delivery.settings import DeliverySettings
from src.linkedin.catalog import SliceSpec, sync_slices
from src.linkedin.collector import collect_once
from src.linkedin.catalog import CollectorConfig
from src.processor.config import load_processor_config
from src.processor.llm_stages import LlmKeys
from src.processor.runner import process_once
from tests.legacy_schema import create_legacy_tables
from tests.linkedin_fakes import FakePace, ScriptedGet, ids, page_html, response
from tests.test_processor_cheap import ENGLISH
from tests.test_processor_llm import FakeScore, FakeVerify

T = datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc)          # the afternoon before
EIGHT_THIRTY = datetime(2026, 10, 14, 6, 30, tzinfo=timezone.utc)  # 08:30 in Rome
FRAGMENT = f'<div class="show-more-less-html__markup">{ENGLISH}</div>'


class Outbox:
    def __init__(self):
        self.sent = []

    def __call__(self, settings, text, *, formatted):
        self.sent.append(text)
        return True


@pytest.fixture(autouse=True)
def _freeze_telegram_date(monkeypatch):
    class Frozen(date):
        @classmethod
        def today(cls):
            return date(2026, 10, 14)
    monkeypatch.setattr(telegram, "date", Frozen)


def test_linkedin_cards_become_a_delivered_digest_notes_alerts_and_an_archive(
        store_conn, store_conn_factory, store_url_fixture, tmp_path):
    cfg = load_processor_config()
    keys = LlmKeys("llm", "groq")

    # 1. collect: one tier-1 slice, three cards
    sync_slices(store_conn, [SliceSpec("linkedin", "AI Engineer", "Italy", 1, 14400)], T)
    store_conn.commit()
    search = ScriptedGet(response(200, page_html(*ids(0, 3))), response(200, page_html()))
    tick = collect_once(store_conn, FakePace(), CollectorConfig(due_hours={1: 4}), now=T, get=search,
                        sleep=lambda _s: None)
    assert tick.did_work and tick.detail["new_jobs"] == 3

    # 2. process: cheap checks and descriptions, then (after the flush wait) verdicts and scores
    fragments = ScriptedGet(*[response(200, FRAGMENT)] * 3)
    kwargs = dict(get=fragments, sleep=lambda _s: None, verify=FakeVerify("confirmed", "fully remote"),
                  score=FakeScore(9))
    process_once(store_conn, FakePace(), cfg, keys, now=T, **kwargs)
    process_once(store_conn, FakePace(), cfg, keys, now=T + timedelta(minutes=31), **kwargs)
    with store_conn.cursor() as cur:
        cur.execute("SELECT stage, tier, score FROM jobs ORDER BY first_seen_at, id")
        assert cur.fetchall() == [("scored", 1, 9)] * 3

    # 3. deliver at 08:30: three alerts, then the four tier digests, reports and the day summary
    settings = DeliverySettings(output_path=str(tmp_path / "output"), delivery_time=time(8, 0),
                                timezone=ZoneInfo("Europe/Rome"))
    schema = "neon_" + uuid.uuid4().hex[:8]
    admin = psycopg2.connect(store_url_fixture)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema}")
    neon = psycopg2.connect(store_url_fixture, options=f"-c search_path={schema}")
    create_legacy_tables(neon)
    init_archive(neon)
    outbox = Outbox()
    service = DeliverService(
        cfg=cfg, settings=settings, packaging=None, pace=FakePace(), tailor_run=None,
        connect_neon=lambda: psycopg2.connect(store_url_fixture, options=f"-c search_path={schema}"), send=outbox)
    try:
        delivered = service.tick(store_conn, EIGHT_THIRTY)
        assert delivered.detail["alerts"] == 3 and delivered.detail["archived"] is True
        assert sum(m.startswith("Strong match") for m in outbox.sent) == 3
        assert outbox.sent[-1].startswith("Day summary - 2026-10-14")

        folder = tmp_path / "output" / "2026-10-14"
        digest = (folder / "tier1" / "digest.md").read_text()
        assert digest.count("sent earlier") == 3 and "# Job Digest - 2026-10-14" in digest
        assert len(list((folder / "tier1" / "scraped").glob("*.md"))) == 3   # the notes alerts wrote
        for tier in (2, 3, 4):
            assert "No new offers after dedup filter." in (folder / f"tier{tier}" / "digest.md").read_text()
        assert (folder / "tier1" / "rejected.md").exists() and not (folder / "tier2" / "rejected.md").exists()

        # 4. archive: the same three jobs, with the same ids, are in Neon
        with neon.cursor() as cur:
            cur.execute("SELECT count(*), count(*) FILTER (WHERE stage = 'delivered') FROM jobs")
            assert cur.fetchone() == (3, 3)
            cur.execute("SELECT count(*) FROM sightings")
            assert cur.fetchone()[0] == 3
        with store_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM jobs WHERE archived_at IS NULL")
            assert cur.fetchone()[0] == 0

        # a second tick the same morning changes nothing
        sent_before = len(outbox.sent)
        service.tick(store_conn, EIGHT_THIRTY + timedelta(minutes=1))
        assert len(outbox.sent) == sent_before
    finally:
        neon.close()
        with admin.cursor() as cur:
            cur.execute(f"DROP SCHEMA {schema} CASCADE")
        admin.close()
