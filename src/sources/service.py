"""One service per extra source: collect-freehire, collect-himalayas.

    python -m src.sources.service freehire --sync-slices   # create the slices, then exit
    python -m src.sources.service freehire --once          # walk one due slice, then exit
    python -m src.sources.service freehire                 # run forever (systemd)

A source ships `enabled: false` in config/sources.json and refuses to run until the
captain switches it on, one source at a time with days of comparison between.
"""
import argparse
import sys

from dotenv import load_dotenv

from src.jobstore import heartbeat
from src.jobstore.db import connect, store_url, utcnow
from src.jobstore.schema import init_store
from src.linkedin.catalog import sync_slices
from src.linkedin.walk import RESUMABLE_STOPS, claim_due_slice
from src.services.runner import Tick, acquire_single_instance, run_service
from src.sources import freehire, himalayas
from src.sources.base import (
    JsonClient, SourceConfig, SourceError, SourceWalk, finish_source_walk, load_source_config,
)

MODULES = {"freehire": freehire, "himalayas": himalayas}

# A daily timer fires at the same clock time every day, but a walk finishes a few seconds
# after it started. Scheduling the next walk an hour short of a full interval keeps the slice
# due when the next day's timer fires instead of skipping every other day.
DUE_SLACK_SECONDS = 3600


def collect_source_once(conn, name: str, cfg: SourceConfig, client: JsonClient, *,
                        now_fn=utcnow, unfinished_only: bool = False) -> Tick:
    started = now_fn()
    row = claim_due_slice(conn, started, name, unfinished_only=unfinished_only)
    if row is None:
        return Tick(False, {"due_slices": 0})
    try:
        walk = MODULES[name].walk_slice(conn, client, cfg, row, started)
    except SourceError as exc:
        conn.rollback()
        print(f"[collect-{name}] {row.role} / {row.area}: {exc}")
        walk = SourceWalk(0, 0, 0, "error", row.resume_start)
    interval_seconds = cfg.interval_hours * 3600
    if unfinished_only and walk.finished:
        # Only the daily run can take a finished slice (resume runs filter on unfinished stop
        # reasons), so make it due well before the next daily start instead of a full interval
        # after this resume run finished it, which would skip a day.
        interval_seconds = DUE_SLACK_SECONDS
    finish_source_walk(conn, row, walk, started, now_fn(),
                       max(interval_seconds - DUE_SLACK_SECONDS, interval_seconds // 2))
    conn.commit()
    print(f"[collect-{name}] {row.role} / {row.area}: {walk.pages} pages, {walk.cards_seen} hits, "
          f"{walk.new_jobs} new, stop={walk.stop_reason}")
    return Tick(True, {"slice": f"{row.role} / {row.area}", "stop_reason": walk.stop_reason,
                       "new_jobs": walk.new_jobs})


def collect_due_slices(conn, name: str, cfg: SourceConfig, client: JsonClient, *,
                       now_fn=utcnow, unfinished_only: bool = False) -> list[Tick]:
    """Drain every slice that is due right now, for a oneshot timer run.

    The daily timer starts the collector once; a single `collect_source_once` would walk
    only the first due slice, so a oneshot run drains them all before exiting. It leaves a
    heartbeat the watchdog reads between runs, marked error if any slice failed. A resume
    run (`unfinished_only`) never touches `beat_at` and only flips an `error` beat to `ok`
    when it recovered slices and no active slice of that source is still in error, so its
    30-minute slots cannot mask a missed daily run."""
    service = f"collect-{name}"
    if not unfinished_only:
        heartbeat.start(conn, service, now=now_fn())
        conn.commit()
    ticks: list[Tick] = []
    error = False
    try:
        while True:
            tick = collect_source_once(conn, name, cfg, client, now_fn=now_fn,
                                       unfinished_only=unfinished_only)
            if not tick.did_work:
                break
            ticks.append(tick)
            error = error or tick.detail.get("stop_reason") == "error"
            if not unfinished_only:
                heartbeat.beat(conn, service, "error" if error else "ok",
                               {"slices": len(ticks),
                                "new_jobs": sum(t.detail.get("new_jobs", 0) for t in ticks)},
                               now=now_fn())
                conn.commit()
    except Exception as exc:  # noqa: BLE001 - a crash must still leave a truthful error beat
        conn.rollback()
        if not unfinished_only:
            heartbeat.beat(conn, service, "error", {"crashed": type(exc).__name__}, now=now_fn())
            conn.commit()
        raise
    if unfinished_only:
        # A resume run never beats (D-k): it only clears an error beat when it actually
        # recovered slices and no active slice of this source is still in error, and it
        # leaves beat_at alone so a missed daily run is still reported.
        if any(t.detail.get("stop_reason") not in RESUMABLE_STOPS for t in ticks):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE service_heartbeats SET state = 'ok', "
                    "detail = detail || '{\"recovered\": true}'::jsonb "
                    "WHERE service = %s AND state = 'error' AND NOT detail ? 'crashed' AND NOT EXISTS ("
                    "SELECT 1 FROM slices WHERE source = %s AND active AND stop_reason = 'error')",
                    (service, name))
            conn.commit()
        return ticks
    if not ticks:
        heartbeat.beat(conn, service, "idle", {"slices": 0}, now=now_fn())
        conn.commit()
    return ticks


def run_oneshot(conn, name: str, cfg: SourceConfig, client: JsonClient, *,
                resume: bool, now_fn=utcnow) -> int:
    """One oneshot run for one source: a daily drain, or a resume run that walks only
    unfinished slices. It takes the per-source single-instance lock, so a resume run and
    the daily run can never request the same source at once, and returns 1 only when every
    slice it walked ended in error."""
    service = f"collect-{name}"
    if not acquire_single_instance(conn, service):
        print(f"[{service}] another run is in progress; exiting")
        return 0
    ticks = collect_due_slices(conn, name, cfg, client, now_fn=now_fn, unfinished_only=resume)
    return 1 if ticks and all(t.detail.get("stop_reason") == "error" for t in ticks) else 0


def main(argv: list[str] | None = None, *, get=None) -> int:
    parser = argparse.ArgumentParser(description="Source collector service")
    parser.add_argument("source", choices=sorted(MODULES))
    parser.add_argument("--sync-slices", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    load_dotenv()
    cfg = load_source_config(args.source)
    if not cfg.enabled:
        print(f"[collect-{args.source}] disabled in config/sources.json; not running")
        return 0
    client = JsonClient(cfg, get=get)
    specs = MODULES[args.source].build_slices(cfg)

    if args.sync_slices or args.once or args.resume:
        conn = connect(store_url())
        try:
            init_store(conn)
            if args.sync_slices or args.once:
                counts = sync_slices(conn, specs, source=args.source)
                conn.commit()
                print(f"[collect-{args.source}] slices: {counts}")
            if args.once or args.resume:
                return run_oneshot(conn, args.source, cfg, client, resume=args.resume)
        finally:
            conn.close()
        return 0

    synced = {"done": False}

    def tick(conn) -> Tick:
        if not synced["done"]:
            sync_slices(conn, specs, source=args.source)
            conn.commit()
            synced["done"] = True
        return collect_source_once(conn, args.source, cfg, client)

    return run_service(f"collect-{args.source}", tick, connect_fn=lambda: connect(store_url()),
                       idle_seconds=300)


if __name__ == "__main__":
    sys.exit(main())
