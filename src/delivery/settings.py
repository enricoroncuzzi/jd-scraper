"""Delivery settings: config/deliver.json plus the environment the old pipeline used."""
import json
import os
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

DELIVER_CONFIG_PATH = "config/deliver.json"


@dataclass(frozen=True)
class DeliverySettings:
    output_path: str
    delivery_time: time
    timezone: ZoneInfo
    archive_every_minutes: int = 30
    alert_poll_seconds: int = 60
    expected_services: tuple[str, ...] = ("collect-linkedin", "process", "deliver")
    telegram_token: str = ""
    telegram_chat_id: str = ""
    neon_url: str | None = None

    def local_day(self, now: datetime):
        """The calendar day in the delivery time zone (the day folders and the
        digest are named by it, as report days already are)."""
        return now.astimezone(self.timezone).date()

    def delivery_started(self, now: datetime) -> bool:
        local = now.astimezone(self.timezone)
        return local.time() >= self.delivery_time


def load_settings(path: str = DELIVER_CONFIG_PATH, env=os.environ) -> DeliverySettings:
    with open(path) as handle:
        data = json.load(handle)
    hour, minute = (int(part) for part in data["delivery_time"].split(":"))
    return DeliverySettings(
        output_path=env.get("OUTPUT_PATH", "output"),
        delivery_time=time(hour, minute),
        timezone=ZoneInfo(data["timezone"]),
        archive_every_minutes=int(data["archive_every_minutes"]),
        alert_poll_seconds=int(data["alert_poll_seconds"]),
        expected_services=tuple(data["expected_services"]),
        telegram_token=env.get("TELEGRAM_TOKEN", ""),
        telegram_chat_id=env.get("TELEGRAM_CHAT_ID", ""),
        neon_url=env.get("DATABASE_URL"),
    )
