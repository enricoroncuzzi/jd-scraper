"""Service liveness rows. The watchdog and the day summary read these."""
import json
from dataclasses import dataclass
from datetime import datetime

from src.jobstore.db import utcnow


@dataclass(frozen=True)
class Heartbeat:
    service: str
    started_at: datetime
    beat_at: datetime
    state: str
    detail: dict


def start(conn, service: str, now: datetime | None = None) -> None:
    """Mark a service as (re)started. The caller commits."""
    now = now or utcnow()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO service_heartbeats (service, started_at, beat_at, state, detail)
            VALUES (%s, %s, %s, 'starting', '{}'::jsonb)
            ON CONFLICT (service) DO UPDATE
               SET started_at = EXCLUDED.started_at, beat_at = EXCLUDED.beat_at,
                   state = 'starting', detail = '{}'::jsonb
            """,
            (service, now, now),
        )


def beat(conn, service: str, state: str = "running", detail: dict | None = None,
         now: datetime | None = None) -> None:
    """Refresh a service's heartbeat. The caller commits."""
    now = now or utcnow()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO service_heartbeats (service, started_at, beat_at, state, detail)
            VALUES (%s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (service) DO UPDATE
               SET beat_at = EXCLUDED.beat_at, state = EXCLUDED.state, detail = EXCLUDED.detail
            """,
            (service, now, now, state, json.dumps(detail or {})),
        )


def read_all(conn) -> list[Heartbeat]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT service, started_at, beat_at, state, detail FROM service_heartbeats "
            "ORDER BY service")
        return [Heartbeat(*row) for row in cur.fetchall()]
