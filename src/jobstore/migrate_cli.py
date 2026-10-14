"""Command line for src/jobstore/migrate.py (wrapped by scripts/migrate_neon_to_jobstore.py)."""
import argparse

import psycopg2

from src.jobstore.db import connect, store_url
from src.jobstore.migrate import format_report, run_migration
from src.jobstore.schema import init_store


def _open_legacy(url: str):
    conn = psycopg2.connect(url)
    conn.set_session(readonly=True)
    return conn


def main(argv: list[str], *, open_legacy=_open_legacy, open_store=None) -> int:
    parser = argparse.ArgumentParser(description="Migrate legacy Neon offers into the job store")
    parser.add_argument("--neon-url", required=True, help="Neon (or Neon branch copy) connection string")
    parser.add_argument("--seen-dir", default=None, help="directory holding seen_tier{1..4}.txt")
    parser.add_argument("--expect-fallback", type=int, default=None,
                        help="expected number of title-only fallback rows (the captain counted 38)")
    parser.add_argument("--commit", action="store_true", help="write for real (default is a dry run)")
    args = parser.parse_args(argv)

    legacy = open_legacy(args.neon_url)
    store = open_store() if open_store else connect(store_url())
    try:
        init_store(store)
        report = run_migration(legacy, store, seen_dir=args.seen_dir, dry_run=not args.commit)
    finally:
        legacy.close()
        store.close()
    print(format_report(report))
    if not args.commit:
        print("DRY RUN: nothing was written.")
    elif report.problems():
        print("NOT COMMITTED: the report does not balance.")
    else:
        print("COMMITTED.")
    if report.problems():
        return 1
    if args.expect_fallback is not None and report.fallback_marked_failed != args.expect_fallback:
        print(f"WARNING: expected {args.expect_fallback} title-only fallback rows, "
              f"found {report.fallback_marked_failed}")
        return 1
    return 0
