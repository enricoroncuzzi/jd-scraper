"""Migrate the legacy Neon tables into the server job store.

    JOBSTORE_URL=postgresql://... python scripts/migrate_neon_to_jobstore.py \
        --neon-url "$NEON_COPY_URL" --seen-dir data            # dry run (default)
    ... --commit                                                # real run

Run it first with a dry run against a copy (Neon branch) of the production data and
a scratch job store. The Neon connection is opened read-only. Exit code 1 means the
report does not balance or the fallback count differs from --expect-fallback.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.jobstore.migrate_cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
