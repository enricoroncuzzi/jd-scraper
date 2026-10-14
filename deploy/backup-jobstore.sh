#!/bin/sh
# Nightly pg_dump of the job store, keeping the last 7 days (the captain's decision).
# JOBSTORE_URL is a libpq connection string; pg_dump takes it directly. Run by
# deploy/systemd/jds-backup.service via deploy/systemd/jds-backup.timer.
set -eu

BACKUP_DIR="${JDS_BACKUP_DIR:-/home/enrico/jd-scraper/backups}"
mkdir -p "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
# Dump to a hidden temp file and only rename it once pg_dump succeeds, so a dump that
# dies midway is never picked up as a backup; the trap removes the partial on failure.
TMP="$BACKUP_DIR/.jobstore-$STAMP.dump.partial"
trap 'rm -f "$TMP"' EXIT
pg_dump --format=custom --file="$TMP" "$JOBSTORE_URL"
mv "$TMP" "$BACKUP_DIR/jobstore-$STAMP.dump"
# Keep exactly the 7 newest dumps, by count, rather than by a -mtime window that can
# hold 8 or 9 depending on when the job runs.
ls -1t "$BACKUP_DIR"/jobstore-*.dump | tail -n +8 | xargs -r rm -f
