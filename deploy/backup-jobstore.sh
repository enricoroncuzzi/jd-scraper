#!/bin/sh
# Nightly pg_dump of the job store, keeping the last 7 days (the captain's decision).
# JOBSTORE_URL is a libpq connection string; pg_dump takes it directly. Run by
# deploy/systemd/jds-backup.service via deploy/systemd/jds-backup.timer.
set -eu

BACKUP_DIR="${JDS_BACKUP_DIR:-/home/enrico/jd-scraper/backups}"
mkdir -p "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
pg_dump --format=custom --file="$BACKUP_DIR/jobstore-$STAMP.dump" "$JOBSTORE_URL"
find "$BACKUP_DIR" -name 'jobstore-*.dump' -type f -mtime +7 -delete
