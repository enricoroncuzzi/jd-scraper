import os
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src import telegram
from src.delivery.service import DeliverService, send_with_fallback
from src.delivery.settings import DeliverySettings
from src.processor.config import load_processor_config
from tests.delivery_helpers import NOW, scored_job
from tests.tailor_helpers import write_cl_master
from tests.linkedin_fakes import FakePace

DAY = date(2026, 10, 14)
BEFORE_EIGHT = datetime(2026, 10, 14, 5, 0, tzinfo=timezone.utc)  # 07:00 Rome


@pytest.fixture(autouse=True)
def _freeze_telegram_date(monkeypatch):
    class Frozen(date):
        @classmethod
        def today(cls):
            return DAY
    monkeypatch.setattr(telegram, "date", Frozen)


class Outbox:
    def __init__(self):
        self.sent = []
        self.fail = False

    def __call__(self, settings, text, *, formatted):
        if self.fail:
            return False
        self.sent.append(text)
        return True


@pytest.fixture
def make(tmp_path, store_conn):
    def build(outbox=None, archive_calls=None):
        settings = DeliverySettings(output_path=str(tmp_path), delivery_time=time(8, 0),
                                    timezone=ZoneInfo("Europe/Rome"), archive_every_minutes=30)
        outbox = outbox or Outbox()
        service = DeliverService(
            cfg=load_processor_config(), settings=settings, packaging=None, pace=FakePace(),
            tailor_run=None, connect_neon=lambda: (_ for _ in ()).throw(RuntimeError("no neon in tests")),
            send=outbox)
        return service, outbox
    return build


def test_before_the_delivery_time_only_alerts_go_out(store_conn, make):
    scored_job(store_conn, 1, 9, when=BEFORE_EIGHT)
    service, outbox = make()
    tick = service.tick(store_conn, BEFORE_EIGHT)
    assert tick.detail["alerts"] == 1 and tick.detail["delivered"] is False
    assert len(outbox.sent) == 1 and outbox.sent[0].startswith("Strong match 9/10")


def test_at_eight_the_four_tier_digests_then_the_day_summary_go_out(store_conn, make):
    scored_job(store_conn, 1, 9, when=BEFORE_EIGHT)
    service, outbox = make()
    service.tick(store_conn, BEFORE_EIGHT)
    outbox.sent.clear()
    tick = service.tick(store_conn, NOW)
    assert tick.did_work and tick.detail["delivered"] is True
    assert len(outbox.sent) == 5  # the four tier digests plus the one day summary
    assert outbox.sent[0].startswith("Hey Enrico! Tier 1")
    assert outbox.sent[3].startswith("Hey Enrico! Tier 4")
    assert outbox.sent[-1].startswith("Day summary - 2026-10-14")
    assert "Tier 1 (Italy): last 24 hours of collection" in outbox.sent[-1]
    assert "sent earlier" in open(os.path.join(service.settings.output_path, "2026-10-14", "tier1",
                                               "digest.md"), encoding="utf-8").read()


def test_a_second_tick_the_same_day_sends_nothing_more(store_conn, make):
    service, outbox = make()
    service.tick(store_conn, NOW)
    count = len(outbox.sent)
    again = service.tick(store_conn, NOW + timedelta(minutes=1))
    assert len(outbox.sent) == count and again.detail["delivered"] is False


def test_a_failed_telegram_send_is_retried_without_redelivering(store_conn, make):
    scored_job(store_conn, 1, 9, when=NOW)
    outbox = Outbox()
    outbox.fail = True
    service, _ = make(outbox)
    service.tick(store_conn, NOW)
    path = os.path.join(service.settings.output_path, "2026-10-14", "tier1", "digest.md")
    good = open(path, encoding="utf-8").read()
    assert outbox.sent == []
    outbox.fail = False
    service.tick(store_conn, NOW + timedelta(minutes=1))
    assert len(outbox.sent) >= 5 and open(path, encoding="utf-8").read() == good
    assert outbox.sent[-1].startswith("Day summary")


def test_the_archive_runs_every_thirty_minutes_and_a_dead_neon_stops_nothing(store_conn, make):
    service, _ = make()
    assert service.tick(store_conn, NOW).detail["archived"] is True
    assert service.tick(store_conn, NOW + timedelta(minutes=10)).detail["archived"] is False
    assert service.tick(store_conn, NOW + timedelta(minutes=31)).detail["archived"] is True


def test_html_rejected_by_telegram_falls_back_to_plain_text(capsys):
    settings = DeliverySettings(output_path="o", delivery_time=time(8), timezone=ZoneInfo("UTC"),
                                telegram_token="t", telegram_chat_id="c")
    calls = []

    def sender(text, token, chat, parse_mode="Markdown"):
        calls.append(parse_mode)
        return parse_mode is None

    assert send_with_fallback(settings, "<b>x</b>", formatted=True, sender=sender) is True
    assert calls == ["HTML", None]
    # The plain-text resend is logged so a silently degraded digest is visible.
    assert "resending as plain text" in capsys.readouterr().out
    assert send_with_fallback(settings, "plain", formatted=False, sender=sender) is True
    assert calls[-1] is None


def test_a_telegram_network_failure_does_not_abort_the_tick(store_conn, tmp_path):
    import requests
    scored_job(store_conn, 1, 9, when=NOW)
    settings = DeliverySettings(output_path=str(tmp_path), delivery_time=time(8, 0),
                                timezone=ZoneInfo("Europe/Rome"))
    archive_calls = []

    def raising_sender(settings, text, *, formatted):
        raise requests.ConnectionError("telegram unreachable")

    service = DeliverService(cfg=load_processor_config(), settings=settings, packaging=None, pace=FakePace(),
                             tailor_run=None, connect_neon=lambda: archive_calls.append(1),
                             send=raising_sender)
    tick = service.tick(store_conn, NOW)
    assert tick.did_work is True  # the delivery itself happened
    assert archive_calls == [1]  # the archive was still attempted
    with store_conn.cursor() as cur:
        cur.execute("SELECT telegram_sent_at FROM delivery_runs WHERE day = %s AND tier = 1", (DAY,))
        assert cur.fetchone()[0] is None
    outbox = Outbox()
    service.send = outbox
    service.tick(store_conn, NOW + timedelta(minutes=1))
    assert len([m for m in outbox.sent if m.startswith("Hey Enrico! Tier")]) == 4


def test_a_persistently_rejected_send_does_not_report_work_forever(store_conn, make):
    scored_job(store_conn, 1, 9, when=NOW)
    outbox = Outbox()
    outbox.fail = True
    service, _ = make(outbox)
    first = service.tick(store_conn, NOW)
    second = service.tick(store_conn, NOW + timedelta(minutes=1))
    assert first.did_work is True  # the delivery itself progressed
    assert second.did_work is False  # every send was rejected, so the loop may idle


def _packaging_service(tmp_path, tailor_run, cv_master, cl_master=None):
    from src.delivery.autopackage import PackagingConfig
    settings = DeliverySettings(output_path=str(tmp_path), delivery_time=time(8, 0),
                                timezone=ZoneInfo("Europe/Rome"))
    pkg = PackagingConfig(str(cv_master), str(tmp_path / "CV_css.md"),
                          str(cl_master) if cl_master else write_cl_master(tmp_path), "key")
    return DeliverService(cfg=load_processor_config(), settings=settings, packaging=pkg, pace=FakePace(),
                          tailor_run=tailor_run, send=Outbox(),
                          connect_neon=lambda: (_ for _ in ()).throw(RuntimeError("no neon in tests")))


def test_a_halted_tailoring_is_retried_with_backoff_and_stops(store_conn, tmp_path):
    (tmp_path / "CV_css.md").write_text("css")
    master = tmp_path / "CV_master.md"
    master.write_text("cv")
    scored_job(store_conn, 1, 9, stage="delivered", note_day=DAY)
    calls = []

    def tailor(note, *a, **kw):
        calls.append(note)
        raise ValueError("HALTED: cover-letter bridge is not concrete")

    service = _packaging_service(tmp_path, tailor, master)
    start = datetime(2026, 10, 14, 6, 0, tzinfo=timezone.utc)
    for minute in range(24 * 60):
        service.tick(store_conn, start + timedelta(minutes=minute))
    assert len(calls) == 3  # 1h, 6h then stop, not once per 15 minutes all day
    assert [m for m in service.send.sent if m.startswith("Auto-apply FAILED")] == []


def test_a_missing_cv_source_sends_one_failure_message_a_day(store_conn, tmp_path):
    scored_job(store_conn, 1, 9, stage="delivered", note_day=DAY)
    service = _packaging_service(tmp_path, lambda *a, **kw: None, tmp_path / "nope.md")
    start = datetime(2026, 10, 14, 6, 0, tzinfo=timezone.utc)  # 08:00 Rome
    for minute in range(8 * 60):  # stays inside one local day
        service.tick(store_conn, start + timedelta(minutes=minute))
    assert len([m for m in service.send.sent if m.startswith("Auto-apply FAILED")]) == 1


def test_a_missing_letter_master_stops_the_pass_before_any_job(store_conn, tmp_path):
    (tmp_path / "CV_css.md").write_text("css")
    master = tmp_path / "CV_master.md"
    master.write_text("cv")
    scored_job(store_conn, 1, 9, stage="delivered", note_day=DAY)
    calls = []
    service = _packaging_service(tmp_path, lambda *a, **kw: calls.append(a), master,
                                 cl_master=tmp_path / "absent_CL_master.md")
    start = datetime(2026, 10, 14, 6, 0, tzinfo=timezone.utc)
    for minute in range(8 * 60):
        service.tick(store_conn, start + timedelta(minutes=minute))
    assert calls == []                    # no job was ever attempted
    failed = [m for m in service.send.sent if m.startswith("Auto-apply FAILED")]
    assert len(failed) == 1 and "CL_MASTER_PATH" in failed[0]
    with store_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM kv_state WHERE key LIKE %s", ("package_fail:%",))
        assert cur.fetchone()[0] == 0     # no backoff row, so no 1h/6h/24h retries


class _Response:
    def __init__(self, ok, status_code, text):
        self.ok = ok
        self.status_code = status_code
        self.text = text


_ALLOWED_HTML_TAGS = ("<b>", "</b>", "<i>", "</i>")
_BARE_AMPERSAND = re.compile(r"&(?!(?:amp|lt|gt|quot|#\d+|#x[0-9a-fA-F]+);)")


def _malformed_html(text: str) -> bool:
    """Telegram's HTML parse mode rejects an unescaped `<`, `>` or `&`, or an
    unbalanced/unknown tag, with 400 "can't parse entities" - the class of
    failure the 2026-10-15 morning digest hit."""
    stripped = text
    for tag in _ALLOWED_HTML_TAGS:
        stripped = stripped.replace(tag, "")
    return "<" in stripped or ">" in stripped or _BARE_AMPERSAND.search(stripped) is not None


def _telegram_simulator():
    calls = []

    def post(url, json, timeout):
        calls.append(json)
        if json.get("parse_mode") == "HTML" and _malformed_html(json["text"]):
            return _Response(False, 400, '{"ok":false,"error_code":400,"description":"Bad Request: '
                                         "can't parse entities: Unsupported start tag")
        return _Response(True, 200, '{"ok":true}')

    post.calls = calls
    return post


def test_a_markup_job_title_reaches_telegram_without_a_plain_text_fallback(store_conn, tmp_path, monkeypatch):
    # The whole day-summary path against a requests.post stand-in that rejects
    # malformed HTML: with every dynamic value escaped, all five messages are
    # accepted on their first (formatted) attempt, so no fallback fires.
    scored_job(store_conn, 1, 9, when=BEFORE_EIGHT,
               title="Senior <AI> *Engineer* & [Remote", company="Acme & Co_Inc",
               location="Milan, Italy", summary="R&D <lead>")
    post = _telegram_simulator()
    monkeypatch.setattr(telegram.requests, "post", post)
    settings = DeliverySettings(output_path=str(tmp_path), delivery_time=time(8, 0),
                                timezone=ZoneInfo("Europe/Rome"), archive_every_minutes=30,
                                telegram_token="t", telegram_chat_id="c")
    service = DeliverService(cfg=load_processor_config(), settings=settings, packaging=None,
                             pace=FakePace(), tailor_run=None,
                             connect_neon=lambda: (_ for _ in ()).throw(RuntimeError("no neon in tests")))
    service.tick(store_conn, BEFORE_EIGHT)  # the immediate alert goes out plain
    post.calls.clear()
    tick = service.tick(store_conn, NOW)
    assert tick.detail["delivered"] is True
    assert len(post.calls) == 5  # the four tier digests plus the day summary
    assert sum(1 for call in post.calls if call.get("parse_mode") == "HTML") == 4
