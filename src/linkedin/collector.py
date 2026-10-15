"""The collect-linkedin service: walk due slices, record sightings, nothing else.

It never fetches a description (the processor does) and never judges a job.

    python -m src.linkedin.collector --sync-slices   # create/refresh the slices, then exit
    python -m src.linkedin.collector --once          # walk one due slice, then exit
    python -m src.linkedin.collector                 # run forever (systemd)
"""
import argparse
import sys
import time
from datetime import datetime

import psycopg2
import requests
from dotenv import load_dotenv

from src.jobstore.db import connect, store_url, utcnow
from src.jobstore.schema import init_store
from src.linkedin.catalog import (
    CollectorConfig, build_slices, load_collector_config, load_tier_configs, sync_slices,
)
from src.linkedin.pace import ManagedPace
from src.linkedin.walk import WalkResult, claim_due_slice, finish_walk, walk_slice
from src.services.legacy_guard import legacy_run_active
from src.services.runner import Tick, run_service

SERVICE = "collect-linkedin"


def _current_resume_start(conn, slice_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT resume_start FROM slices WHERE id = %s", (slice_id,))
        return cur.fetchone()[0]


def collect_once(conn, pace, cfg: CollectorConfig, *, now: datetime | None = None,
                 get=requests.get, sleep=time.sleep) -> Tick:
    """Walk the next due slice, if any.

    One slice's unexpected failure must not wedge the collector on a poison card:
    it becomes a retryable `error` (due again in 30 minutes, resuming where the
    last committed page stopped) and the next slice gets its turn. A connection
    that is actually gone is re-raised for the runner to reconnect.
    """
    started = now or utcnow()
    row = claim_due_slice(conn, started)
    if row is None:
        return Tick(False, {"due_slices": 0})
    try:
        result = walk_slice(conn, pace, row, cfg, started, get=get, sleep=sleep)
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        raise
    except Exception as exc:  # noqa: BLE001 - a poison slice must not stop the service
        conn.rollback()
        print(f"[{SERVICE}] tier {row.tier} {row.role} / {row.area} failed: "
              f"{type(exc).__name__}: {exc}")
        result = WalkResult(0, 0, 0, "error", 0, _current_resume_start(conn, row.id))
    finish_walk(conn, row, result, started, now or utcnow())
    conn.commit()
    print(f"[{SERVICE}] tier {row.tier} {row.role} / {row.area}: {result.pages} pages, "
          f"{result.cards_seen} cards, {result.new_jobs} new, stop={result.stop_reason}")
    return Tick(True, {"slice": f"{row.role} / {row.area}", "stop_reason": result.stop_reason,
                       "new_jobs": result.new_jobs})


def make_tick(pace, cfg: CollectorConfig, *, get=requests.get, sleep=time.sleep):
    def tick(conn) -> Tick:
        if legacy_run_active(cfg.legacy_lock_path):
            return Tick(False, {"waiting": "legacy cron run holds the lock"})
        return collect_once(conn, pace, cfg, get=get, sleep=sleep)
    return tick


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LinkedIn collector service")
    parser.add_argument("--sync-slices", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    load_dotenv()
    cfg = load_collector_config()
    specs = build_slices(load_tier_configs(), cfg.due_hours, cfg.area_due_hours, cfg.skip_slices)

    if args.sync_slices or args.once:
        conn = connect(store_url())
        try:
            init_store(conn)
            counts = sync_slices(conn, specs)
            conn.commit()
            print(f"[{SERVICE}] slices: {counts}, total configured {len(specs)}")
            if args.once:
                pace = ManagedPace(lambda: connect(store_url()))
                collect_once(conn, pace, cfg)
        finally:
            conn.close()
        return 0

    def sync_then_tick(conn) -> Tick:
        counts = sync_slices(conn, specs)
        conn.commit()
        if any(counts.values()):
            print(f"[{SERVICE}] slices: {counts}")
        return Tick(False)

    pace = ManagedPace(lambda: connect(store_url()))
    tick = make_tick(pace, cfg)
    first = {"done": False}

    def tick_with_first_sync(conn) -> Tick:
        if not first["done"]:
            sync_then_tick(conn)
            first["done"] = True
        return tick(conn)

    return run_service(SERVICE, tick_with_first_sync, connect_fn=lambda: connect(store_url()))


if __name__ == "__main__":
    sys.exit(main())
