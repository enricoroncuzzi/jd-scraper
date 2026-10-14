# jd-scraper services: runbook

How the continuous pipeline is installed, switched on, watched, rolled back and
deployed. The design is in the redesign spec; this file is the procedure. Every command
is for the production server (Debian 13, user `enrico`, repo `/home/enrico/jd-scraper`)
unless it says otherwise. Nothing here is run by a test.

## 1. What runs

| Unit | Does | Talks to |
|---|---|---|
| `jds-collect-linkedin` | walks LinkedIn search slices, records sightings | LinkedIn (search), Postgres |
| `jds-process` | cheap checks, descriptions, language, remote check, scoring | LinkedIn (descriptions), OpenRouter, Groq, Postgres |
| `jds-deliver` | alerts, daily delivery at 08:00 Rome time, packaging, Neon copy | Telegram, Neon, Postgres |
| `jds-collect-freehire.timer`, `jds-collect-himalayas.timer` | one oneshot pull a day each (stage 2), clear of delivery and of each other | their public APIs, Postgres |
| `jds-watchdog.timer` | every 10 minutes: alerts when a service or the store is down | Telegram, Postgres |
| `jds-backup.timer` | nightly `pg_dump` of the job store, keeping 7 days | Postgres, local disk |

Services never call each other. Their only shared state is the PostgreSQL database
`jds` on the same machine (`JOBSTORE_URL`). Neon (`DATABASE_URL`) is an archive that
`jds-deliver` copies finished jobs into.

Look at everything with: `cd /home/enrico/jd-scraper && .venv/bin/python -m src.services.status`

## 2. One-time provisioning (root)

```bash
apt-get install -y postgresql            # PostgreSQL 17 on Debian 13
sudo -u postgres createuser enrico
sudo -u postgres createdb -O enrico jds
cat >/etc/postgresql/17/main/conf.d/jds.conf <<'CONF'
shared_buffers = 128MB
work_mem = 4MB
maintenance_work_mem = 32MB
effective_cache_size = 384MB
max_connections = 30
CONF
systemctl restart postgresql
sudo -u enrico psql jds -c 'select version()'      # peer authentication over the unix socket, no password
```

As `enrico`, add to `/home/enrico/jd-scraper/.env` (never commit it):

```
JOBSTORE_URL=postgresql:///jds
```

Then:

```bash
cd /home/enrico/jd-scraper
git pull --ff-only
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m src.delivery.archive --init       # creates the archive tables in Neon (idempotent)
sudo cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload                           # do NOT enable anything yet
sudo systemd-analyze verify /etc/systemd/system/jds-*.service
```

`verify` only comes back clean once the live tree holds this release: `jds-backup.service` runs
`/home/enrico/jd-scraper/deploy/backup-jobstore.sh`. Units installed from a separate checkout
before the deploy report exactly that one missing file.

### Sizing the memory caps from measurement

`MemoryMax` in the unit files is a first guess. The box has 1.9 GB and no swap. For now
jd-scraper has the whole server (the other project is expected to move to its own box), so
the ceilings are sized for jd-scraper plus PostgreSQL plus the operating system (kernel,
systemd, sshd, the watchdog oneshot and a `pg_dump`): the test's `SYSTEM_RESERVE_MB` is 250 MB
and the `OTHER_WORKLOAD_MB` reserve for anything else that shares the machine stays zero until
that changes. The first-guess ceilings leave about 34 MB of slack on top of that. Measure
before trusting the guess:

1. On a quiet day run each service for an hour under load (`systemctl start`, let it work).
2. Read the peak: `systemctl show -p MemoryPeak jds-process jds-collect-linkedin jds-deliver`.
3. For `jds-deliver`, force a tailoring run during the hour (a job scoring 8 or more) so the
   Chromium peak is included.
4. Set `MemoryMax` to about 1.3 times the peak and `MemoryHigh` to about 1.1 times, edit the
   unit files in the repo, and keep the sum plus Postgres (about 400 MB) plus the operating
   system's share (about 250 MB) below the box size minus whatever is reserved for other
   workloads.

## 3. Rehearsal (days before the switch)

On a machine that can reach a Neon branch copy of production (ask for a branch; never point
this at production data you cannot recreate):

```bash
sudo -u postgres createdb -O enrico jds_rehearsal     # the enrico role has no CREATEDB
JOBSTORE_URL=postgresql:///jds_rehearsal .venv/bin/python scripts/migrate_neon_to_jobstore.py \
    --neon-url "$NEON_BRANCH_URL" --seen-dir data --expect-fallback 38
```

This is a dry run. It must end `DRY RUN: nothing was written.` with no `PROBLEM:` lines:
every `offers` row, application and label accounted for. The title-only fallback count (38 when
the captain counted it) is a snapshot that grows with each daily run: a higher count prints
`WARNING: expected 38 ...` and exits 1 although nothing is wrong. Check that the extra rows
are recent (`description = title || ' at ' || company`, grouped by run day); the success test
is `DRY RUN: nothing was written.` / `COMMITTED.` and no `PROBLEM:` line.
Run it again with `--commit` against the rehearsal database and keep the printed report.

## 4. Switch-over day

The old and new systems must never touch LinkedIn together. The collector also refuses to
walk while the cron wrapper holds `/home/enrico/.jd-scraper.lock`, but do not rely on that.

1. Wait for the morning cron run to finish. It has finished when
   `flock -n /home/enrico/.jd-scraper.lock true && echo free` prints `free`.
2. Stop cron from starting another: `crontab -l > /home/enrico/crontab.bak-$(date +%F)`, then
   `crontab -e` and comment out the `jds-daily.sh` line.
3. Deploy the code if it is not already there (`git pull --ff-only`, `pip install -r requirements.txt`).
4. Migrate for real:
   `JOBSTORE_URL=postgresql:///jds .venv/bin/python scripts/migrate_neon_to_jobstore.py --neon-url "$(grep '^DATABASE_URL=' .env | cut -d= -f2-)" --seen-dir data --expect-fallback 38 --commit`
   It must print `COMMITTED.` (a `WARNING:` about the fallback count alone is expected drift,
   see section 3). If it prints `NOT COMMITTED`, stop: nothing was written. Do not `source .env`
   in the shell: an unquoted `&` in the URL cuts it short. The Python entry points read `.env`
   themselves, except this script, which only reads the environment.
5. Create the slices and check the count (132 for the four tiers and four roles):
   `.venv/bin/python -m src.linkedin.collector --sync-slices`
6. Start, in this order, checking `status` after each:
   `sudo systemctl enable --now jds-process jds-collect-linkedin jds-deliver jds-watchdog.timer jds-backup.timer`
7. Within ten minutes `status` must show all three services `running` or `idle`, slices with a
   recent `last walk`, and jobs appearing under `seen`. Watch
   `journalctl -u jds-collect-linkedin -f` for the first pages.
8. Over the next days compare with the old reports: jobs per tier, scores, the format of the
   first delivered day (`.venv/bin/python -m src.delivery.golden_check output/<date>`), and run
   `jd-sync` on the Mac to confirm the day folder arrives as before.

### Enabling a stage-2 source (after its provenance report)

Each extra source ships disabled. Once its provenance report has been read and the captain
approves it, one source at a time (shown for freehire; for Himalayas substitute `himalayas`
in the config key, the service argument and the unit names):

```bash
.venv/bin/python -m src.sources.provenance 7        # read this before enabling
# edit config/sources.json: "freehire": {"enabled": true, ...}   <- must come first:
# `--sync-slices` returns early while the source is still disabled
.venv/bin/python -m src.sources.service freehire --sync-slices
sudo cp deploy/systemd/jds-collect-freehire.service deploy/systemd/jds-collect-freehire.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jds-collect-freehire.timer
```

The service is a oneshot run by its timer (freehire at 03:00, Himalayas at 04:00), clear of
delivery and of the other collectors; enable the timer, not the service (the service has no `[Install]` section on
purpose). Each run records a heartbeat, so the watchdog allows the collector its
`interval_hours` plus a few hours of silence between runs. Disable it with the other timers
in section 5.

## 5. Rollback

Possible at any time in the first two weeks; the old code is untouched.

```bash
sudo systemctl disable --now jds-deliver jds-process jds-collect-linkedin \
    jds-watchdog.timer jds-backup.timer jds-collect-freehire.timer jds-collect-himalayas.timer
crontab /home/enrico/crontab.bak-<date>            # restores the jds-daily.sh line
```

A stage-2 timer that was never installed (section 4) makes systemd print "unit not found" for
it; the rest are still disabled.

Jobs the new system already handled would be found again by the next old run. To avoid
repeats, append their hashes to the old dedup files first (one command per tier N = 1..4):

```bash
psql jds -At -c "SELECT md5(s.source_url) FROM sightings s JOIN jobs j ON j.id = s.job_id
  WHERE s.source = 'linkedin' AND j.tier = N AND j.stage IN ('scored','delivered','filtered_out')" \
  >> data/seen_tierN.txt
```

The job store and the Neon archive are left as they are, so switching forward again later
loses nothing.

## 6. Daily operation

- Health: the day summary carries each service's state; the watchdog messages Telegram if a
  service is silent for over an hour, nothing was collected for six hours, or Postgres is down.
- Logs: `journalctl -u jds-process --since today`. LinkedIn throttling shows up as
  `spacing x` above 1.00 in `status`; it clears by itself.
- Language-model use: the day summary shows OpenRouter requests against 1,000. It turns
  `HIGH` from 800. A budget that prioritises tier 1 is built only if usage exceeds 800 a day
  on three days in a fortnight (spec section 6.5).
- Deploying new code: `git pull --ff-only && .venv/bin/pip install -r requirements.txt`, then
  restart one unit at a time (`sudo systemctl restart jds-process`; wait for `status` to show
  it healthy; then the next). A restart sends SIGTERM and the service stops between jobs, so it
  waits at most `TimeoutStopSec` (5 minutes) and then systemd kills it; killing mid-job is safe
  because every stage commits per job or per batch. Database changes are additive and applied
  by `init_store` when a service starts.
- Voluntary stop of LinkedIn requests: `sudo systemctl stop jds-collect-linkedin jds-process`.

## 7. Backups (captain decision)

The job store is backed up nightly by `jds-backup.timer` (03:30, `Persistent=true`), which
runs `deploy/backup-jobstore.sh`: a `pg_dump` of `JOBSTORE_URL` into
`/home/enrico/backups/jobstore-<stamp>.dump`, then keeps the 7 newest dumps. It is enabled
with the other units in section 4 and disabled with them in section 5. The backup directory
defaults to `/home/enrico/backups`, outside the repository so a deploy-time `git clean -fdx`
cannot delete it, and can be moved with `JDS_BACKUP_DIR`.

The job store holds only jobs in flight plus a copy of what Neon already has, so the backup is
a cheap safety net rather than the system of record. To restore, stop the services, then
`pg_restore --clean --if-exists -d jds /home/enrico/backups/jobstore-<newest>.dump` before
starting them again. Copy the dumps off the box if losing the day's unfinished work would
matter.

## 8. Retirement of the old pipeline (after two stable weeks)

Not part of the switch-over. Remove `orchestrator.py`, `main.py`, `src/scraper.py`,
`src/dedup.py`, `src/retry_queue.py`, `src/autoapply/pipeline.py`, the write functions of
`src/writer.py`, the session code in `src/telemetry.py`, `scripts/run_report.py`, the
`data/seen_tier*.txt` and `data/unscored_*.jsonl` files, the cron wrapper, and the legacy
sections of `AGENTS.md` and `README.md`. Keep what the new code still imports: the
formatters in `src/writer.py` (`_format_note`, `_format_digest`, `_note_filename`, `_slugify`),
`TIER_NAMES` and `TIER_FLAGS` from `src/report_data.py`, `pack_messages` from
`src/report_render.py`, and `src/telegram.py::_format_message`; move those into
`src/delivery/` first so the old modules can go. Do it in its own pull request with the
suite green.
