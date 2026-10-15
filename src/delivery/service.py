"""The deliver service: immediate alerts every minute, the daily delivery at the
fixed time, packaging, and the Neon batch copy.

A day sends the four tier digest messages plus one day summary (the captain's
Telegram volume decision); the per-tier collection reports are folded into the day
summary by src/delivery/messages.py rather than sent separately.

    python -m src.delivery.service --once    # one tick, then exit
    python -m src.delivery.service           # run forever (systemd)
"""
import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg2
from dotenv import load_dotenv

from src import telemetry
from src.delivery.alerts import run_alerts
from src.delivery.archive import archive_once
from src.delivery.autopackage import PackagingConfig, run_packaging
from src.delivery.daily import (
    TierDelivery, already_delivered, deliver_tier, delivered_jobs, delivery_pending,
    mark_telegram_sent, unsent_telegram, waiting_count,
)
from src.delivery.messages import render_day_summary, tier_digest_message
from src.delivery.settings import DeliverySettings, load_settings
from src.jobstore.db import connect, store_url, utcnow
from src.linkedin.pace import ManagedPace
from src.linkedin.schedule import load_schedule_config
from src.processor.config import ProcessorConfig, load_processor_config
from src.processor.llm_sink import StoreLlmSink
from src.services.runner import Tick, run_service
from src.telegram import HTML_PARSE_MODE, send_message

SERVICE = "deliver"


def send_with_fallback(settings: DeliverySettings, text: str, *, formatted: bool,
                       sender=send_message) -> bool:
    """HTML first (the digest message is built with <b>/<i>), plain text if Telegram
    rejects the markup. Total: a network error, timeout or rejection never propagates, so a
    Telegram outage cannot abort the tick and stop packaging or the archive."""
    if formatted:
        try:
            if sender(text, settings.telegram_token, settings.telegram_chat_id,
                      parse_mode=HTML_PARSE_MODE):
                return True
            print("[deliver] formatted send rejected; resending as plain text")
        except Exception as exc:  # noqa: BLE001 - fall through to the plain-text attempt
            print(f"[deliver] formatted send failed ({type(exc).__name__}: {exc}); trying plain text")
    try:
        return bool(sender(text, settings.telegram_token, settings.telegram_chat_id, parse_mode=None))
    except Exception as exc:  # noqa: BLE001 - a Telegram outage must not abort the tick
        print(f"[deliver] telegram send failed ({type(exc).__name__}: {exc})")
        return False


@dataclass
class DeliverService:
    cfg: ProcessorConfig
    settings: DeliverySettings
    packaging: PackagingConfig | None
    pace: object
    tailor_run: object
    connect_neon: object
    send: object = send_with_fallback
    last_archive: datetime | None = None

    def _send(self, text: str, formatted: bool = False) -> bool:
        try:
            return self.send(self.settings, text, formatted=formatted)
        except Exception as exc:  # noqa: BLE001 - a Telegram outage must not abort the tick
            print(f"[deliver] telegram send failed ({type(exc).__name__}: {exc})")
            return False

    def _day_summary_sent(self, conn, day) -> bool:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM kv_state WHERE key = %s", (f"summary:{day}",))
            return cur.fetchone() is not None

    def _mark_summary_sent(self, conn, day, now) -> None:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO kv_state (key, value, updated_at) VALUES (%s, '{}'::jsonb, %s) "
                        "ON CONFLICT (key) DO NOTHING", (f"summary:{day}", now))
        conn.commit()

    def _send_tier_digest(self, conn, tier: int, delivery: TierDelivery, day, now) -> bool:
        policy = self.cfg.policies[tier]
        ok = self._send(tier_digest_message(delivery, policy), formatted=True)
        if ok:
            mark_telegram_sent(conn, day, tier, now)
        return ok

    def _daily(self, conn, now: datetime) -> bool:
        tiers = sorted(self.cfg.policies)
        day = delivery_pending(conn, self.settings, tiers, now)
        local_day = self.settings.local_day(now)
        did = False
        deliveries: dict[int, TierDelivery] = {}
        if day is not None:
            for tier in tiers:
                delivery = deliver_tier(conn, self.cfg, self.settings, tier, day, now)
                if delivery is not None:
                    deliveries[tier] = delivery
                    did = True
        for tier in unsent_telegram(conn, local_day):
            delivery = deliveries.get(tier) or TierDelivery(
                tier, local_day, delivered_jobs(conn, local_day, tier), [], waiting_count(conn, tier))
            did = self._send_tier_digest(conn, tier, delivery, local_day, now) or did
            deliveries.setdefault(tier, delivery)
        everything_delivered = day is None and all(
            already_delivered(conn, local_day, t) for t in tiers) and self.settings.delivery_started(now)
        if (day is not None or everything_delivered) and not unsent_telegram(conn, local_day) \
                and not self._day_summary_sent(conn, local_day):
            for tier in tiers:
                deliveries.setdefault(tier, TierDelivery(
                    tier, local_day, delivered_jobs(conn, local_day, tier), [], waiting_count(conn, tier)))
            messages = render_day_summary(conn, self.cfg, self.settings, deliveries, now, local_day)
            if all([self._send(message) for message in messages]):
                self._mark_summary_sent(conn, local_day, now)
                did = True
        return did

    def _package(self, conn, now: datetime) -> int:
        if self.packaging is None:
            return 0
        try:
            return len(run_packaging(conn, self.cfg, self.settings, self.packaging, self.pace,
                                     now=now, tailor_run=self.tailor_run))
        except Exception as exc:  # noqa: BLE001 - a packaging failure must not stop delivery
            print(f"[deliver] packaging failed ({type(exc).__name__}: {exc})")
            self._notify_failure_once(conn, now, exc)
            return 0

    def _notify_failure_once(self, conn, now, exc) -> None:
        """One Auto-apply FAILED message per day per error class, not one per pass."""
        day = self.settings.local_day(now)
        key = f"autoapply_failed:{day}:{type(exc).__name__}"
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM kv_state WHERE key = %s", (key,))
            if cur.fetchone():
                return
            cur.execute("INSERT INTO kv_state (key, value, updated_at) VALUES (%s, '{}'::jsonb, %s) "
                        "ON CONFLICT (key) DO NOTHING", (key, now))
        conn.commit()
        self._send(f"Auto-apply FAILED: {type(exc).__name__}: {exc}"[:500])

    def _archive(self, conn, now: datetime) -> bool:
        interval = timedelta(minutes=self.settings.archive_every_minutes)
        if self.last_archive is not None and now - self.last_archive < interval:
            return False
        self.last_archive = now
        archive_once(conn, self.connect_neon, now)
        return True

    def _packaging_due(self, now: datetime) -> bool:
        """Retry failed packages about every 15 minutes, not every tick."""
        return now.minute % 15 == 0

    def tick(self, conn, now: datetime | None = None) -> Tick:
        now = now or utcnow()
        alerts = run_alerts(conn, self.cfg, self.settings, lambda text: self._send(text), now)
        daily = self._daily(conn, now)
        packaged = self._package(conn, now) if (alerts or daily or self._packaging_due(now)) else 0
        archived = self._archive(conn, now)
        return Tick(bool(alerts or daily or packaged),
                    {"alerts": alerts, "delivered": daily, "packaged": packaged, "archived": archived})


def build_service() -> DeliverService:
    import os
    cfg = load_processor_config()
    settings = load_settings()
    pace = ManagedPace(lambda: connect(store_url()), service=SERVICE,
                       schedule=load_schedule_config())
    packaging = None
    tailor_run = None
    if any(p.autoapply_enabled for p in cfg.policies.values()):
        import tailor as tailor_cli
        packaging = PackagingConfig(tailor_cli._DEFAULT_MASTER, tailor_cli._DEFAULT_CSS,
                                    os.environ["LLM_API_KEY"])
        tailor_run = tailor_cli.run
    neon_url = settings.neon_url
    return DeliverService(cfg, settings, packaging, pace, tailor_run,
                          connect_neon=lambda: psycopg2.connect(neon_url))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Delivery service")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    load_dotenv()
    telemetry.set_llm_sink(StoreLlmSink(lambda: connect(store_url()), SERVICE))
    service = build_service()
    if args.once:
        conn = connect(store_url())
        try:
            print(service.tick(conn).detail)
        finally:
            conn.close()
        return 0
    return run_service(SERVICE, service.tick, connect_fn=lambda: connect(store_url()),
                       idle_seconds=service.settings.alert_poll_seconds)


if __name__ == "__main__":
    sys.exit(main())
