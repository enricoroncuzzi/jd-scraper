"""The 96 h age-out: report what it would expire, or undo what it expired.

    python -m src.processor.age_out --dry-run   # the default: count, write nothing
    python -m src.processor.age_out --undo      # restore aged_out jobs to seen

`jds-process` is the only writer of the rule: `src/processor/describe.py`'s `expire_stale`
runs it every tick, so the first tick after a deploy is the day 0 reset. This CLI exists for
the two things an operator needs around that: see the count before the tick takes it, and put
the jobs back if the rule was wrong.

The undo writes exactly what the age-out wrote (`stage`, `stage_reason`, `stage_changed_at`)
and clears `archived_at` so a job already copied to Neon is copied again when it finishes. It
does not touch `next_attempt_at`, the description, the score or the verdict, so a job in a
refusal backoff keeps its backoff. It works with the rule switched off, which is the way to
make the undo stick: the next tick would otherwise re-expire the same still-old rows.
"""
import argparse
import sys
from dataclasses import replace
from datetime import datetime, timedelta

from dotenv import load_dotenv

from src.jobstore.db import connect, store_url, utcnow
from src.processor.config import ProcessorConfig, load_processor_config
from src.processor.describe import age_out_where

DEFAULT_ACTION = "dry_run"


def dry_run(conn, cfg: ProcessorConfig, now: datetime) -> list[tuple[int, int | None, int]]:
    """Every job the rule would expire right now, by tier and class. Reads only."""
    where, params = age_out_where(cfg)
    cutoff = now - timedelta(hours=cfg.age_out_hours)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT tier, description_class, count(*) FROM jobs WHERE {where} "
            "AND first_seen_at < %s GROUP BY 1, 2 ORDER BY 1, 2",
            (*params, cutoff))
        rows = [(tier, rank, count) for tier, rank, count in cur.fetchall()]
    if not cfg.age_out_enabled:
        print("age-out is disabled (age_out.enabled: false)")
        rows = []
    total = 0
    for tier, rank, count in rows:
        name = cfg.fetch_order.name(tier, rank)
        label = f"class {rank}" if name == "unclassified" else f"class {rank} ({name})"
        print(f"  tier {tier} {label}: {count}")
        total += count
    print(f"  total: {total}")
    return rows


def undo(conn) -> int:
    """Restore every aged-out job to `seen`, exactly as the age-out left it."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET stage = 'seen', stage_reason = '', stage_changed_at = now(), "
            "archived_at = NULL WHERE stage = 'expired' AND stage_reason = 'aged_out'")
        restored = cur.rowcount
    conn.commit()
    print(f"Restored {restored} jobs from aged_out to seen")
    return restored


def main(argv=None, *, conn=None, cfg: ProcessorConfig | None = None,
         now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.processor.age_out",
        description="Report the 96 h age-out (default) or undo it.")
    parser.add_argument("--dry-run", action="store_true",
                        help="count the jobs the rule would expire, write nothing (default)")
    parser.add_argument("--undo", action="store_true",
                        help="restore aged_out jobs to seen")
    args = parser.parse_args(argv)
    if args.dry_run and args.undo:
        parser.error("--dry-run and --undo are exclusive")

    cfg = cfg or load_processor_config()
    now = now or utcnow()
    own = conn is None
    if own:
        load_dotenv()
        conn = connect(store_url())
    try:
        if args.undo:
            undo(conn)
        else:
            dry_run(conn, cfg, now)
    finally:
        if own:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
