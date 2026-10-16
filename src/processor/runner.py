"""The process service: move jobs through their stages, continuously.

Each tick runs the stages in order: expire stale jobs, cheap checks, no-model
verdict rules, the remote check (or the merged call when switched on), scoring,
and last the description fetches (shared LinkedIn pace). Every stage commits per job or per
batch, so a restart resumes exactly where it stopped.

    python -m src.processor.runner --once    # one tick, then exit
    python -m src.processor.runner           # run forever (systemd)
"""
import argparse
import sys
import time
from datetime import datetime

import requests
from dotenv import load_dotenv

from src import telemetry
from src.http_session import new_session
from src.jobstore.db import connect, store_url, utcnow
from src.linkedin.pace import linkedin_pace
from src.linkedin.schedule import load_schedule_config
from src.processor.cheap import run_cheap_checks
from src.processor.combined import run_combined_batches
from src.processor.config import ProcessorConfig, load_processor_config
from src.processor.describe import expire_stale, run_descriptions
from src.processor.llm_sink import StoreLlmSink
from src.processor.llm_stages import LlmKeys
from src.processor.remote import apply_deterministic_rules, run_remote_batches
from src.processor.scoring import run_scoring_batches
from src.remote_verifier import verify_offers
from src.scorer import score_offers
from src.services.runner import Tick, run_service

SERVICE = "process"


def waiting_counts(conn) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT stage, count(*) FROM jobs WHERE stage IN ('seen', 'described') GROUP BY stage")
        return {stage: count for stage, count in cur.fetchall()}


def process_once(conn, pace, cfg: ProcessorConfig, keys: LlmKeys, *, now: datetime | None = None,
                 get=requests.get, sleep=time.sleep, verify=verify_offers, score=score_offers,
                 chain=None) -> Tick:
    now = now or utcnow()
    expired = expire_stale(conn, cfg, now)
    conn.commit()
    cheap = run_cheap_checks(conn, cfg, now)
    settled = apply_deterministic_rules(conn, cfg, now)
    if cfg.merged_call_enabled:
        checked = run_combined_batches(conn, cfg, now, keys, chain=chain, sleep=sleep)
    else:
        checked = run_remote_batches(conn, cfg, now, keys, verify=verify)
    scored = run_scoring_batches(conn, cfg, now, keys, score=score)
    # LinkedIn last: when the pace is slow, a tick with other work must not sit in its queue.
    busy = any((cheap, settled, checked, scored))
    fetched = run_descriptions(conn, pace, cfg, now, get=get, sleep=sleep,
                               skip_if_wait_over=cfg.description_seconds_per_tick if busy else None)
    detail = {"expired": expired, "cheap": cheap, "settled": settled, "described": fetched,
              "checked": checked, "scored": scored, "waiting": waiting_counts(conn)}
    return Tick(any((cheap, settled, fetched, checked, scored)), detail)


def make_tick(pace, cfg: ProcessorConfig, keys: LlmKeys, *, get=None):
    """Build the processor's tick, reusing one LinkedIn HTTP session for every request
    in every tick, so no connection is reopened per description."""
    if get is None:
        get = new_session().get

    def tick(conn) -> Tick:
        return process_once(conn, pace, cfg, keys, get=get)
    return tick


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Processor service")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    load_dotenv()
    cfg = load_processor_config()
    keys = LlmKeys.from_env()
    telemetry.set_llm_sink(StoreLlmSink(lambda: connect(store_url()), SERVICE))
    pace = linkedin_pace(SERVICE, load_schedule_config(), cfg.description_seconds_per_tick)
    tick = make_tick(pace, cfg, keys)

    if args.once:
        conn = connect(store_url())
        try:
            print(tick(conn).detail)
        finally:
            conn.close()
        return 0
    return run_service(SERVICE, tick, connect_fn=lambda: connect(store_url()), idle_seconds=120)


if __name__ == "__main__":
    sys.exit(main())
