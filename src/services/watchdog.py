"""The watchdog: a oneshot run by a systemd timer every ten minutes.

It lives outside the services it watches, so it can report a dead `deliver`. It sends
Telegram alerts when a service has been silent for over an hour or nothing was
collected for six hours (src/delivery/health.py), and when the job store itself
cannot be reached, which it can only remember in a local file because the database
that normally holds its ledger is the thing that is down.
"""
import json
import os
import sys
from datetime import datetime

from dotenv import load_dotenv

from src.delivery.health import REPEAT_ALERT_AFTER, run_health_check
from src.delivery.settings import DeliverySettings, load_settings
from src.jobstore.db import connect, store_url, utcnow
from src.telegram import send_message

STATE_PATH = "data/watchdog_state.json"
SOURCES_CONFIG_PATH = "config/sources.json"


def enabled_sources(path: str = SOURCES_CONFIG_PATH) -> list[str]:
    """Extra sources switched on in config/sources.json; none when the file is absent."""
    try:
        with open(path) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    return sorted(name for name, cfg in data.items() if isinstance(cfg, dict) and cfg.get("enabled"))


def expected_services(settings: DeliverySettings, sources: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(settings.expected_services + tuple(f"collect-{name}" for name in sources)))


def _store_down_due(path: str, now: datetime) -> bool:
    try:
        with open(path) as handle:
            last = datetime.fromisoformat(json.load(handle)["store_down_sent_at"])
    except (OSError, KeyError, ValueError):
        return True
    return now - last >= REPEAT_ALERT_AFTER


def _remember_store_down(path: str, now: datetime) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as handle:
        json.dump({"store_down_sent_at": now.isoformat()}, handle)


def _forget_store_down(path: str) -> bool:
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def run(settings: DeliverySettings, send, *, connect_fn=lambda: connect(store_url()),
        state_path: str = STATE_PATH, now: datetime | None = None) -> list[str]:
    now = now or utcnow()
    try:
        conn = connect_fn()
    except Exception as exc:  # noqa: BLE001 - the store being down is exactly what we report
        if _store_down_due(state_path, now):
            text = f"ALERT: the job store is unreachable ({type(exc).__name__}: {str(exc)[:120]})"
            if send(text) is not False:
                _remember_store_down(state_path, now)
                return [text]
        return []
    try:
        sent = run_health_check(conn, settings, send, now)
        if _forget_store_down(state_path):
            text = "OK again: the job store is reachable"
            if send(text) is not False:
                sent.append(text)
        return sent
    finally:
        conn.close()


def main() -> int:
    load_dotenv()
    settings = load_settings()
    settings = DeliverySettings(
        **{**settings.__dict__, "expected_services": expected_services(settings, enabled_sources())})

    def send(text: str):
        return send_message(text, settings.telegram_token, settings.telegram_chat_id, parse_mode=None)

    for line in run(settings, send):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
