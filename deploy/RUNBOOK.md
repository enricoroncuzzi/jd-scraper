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
  service is silent for over an hour, a daily source has missed its run or ended in error, no
  LinkedIn request has succeeded for 24 h of open time, or Postgres is down.
- Logs: `journalctl -u jds-process --since today`. A refusal shows up as a
  `[pace] LinkedIn refused a ... request ... waiting Ns (refusal K in a row)` line, and the
  `status` pace line shows the hour nudge, the refusals in a row and any wait (`waiting until
  ... Rome`). There is no global rest state: a refusal is a short capped wait the pace applies
  before the next request, so the system never shuts itself off for hours.
- Language-model use: the day summary shows OpenRouter requests against 1,000. It turns
  `HIGH` from 800. A budget that prioritises tier 1 is built only if usage exceeds 800 a day
  on three days in a fortnight (spec section 6.5).
- Deploying new code: `git pull --ff-only && .venv/bin/pip install -r requirements.txt`, then
  restart one unit at a time (`sudo systemctl restart jds-process`; wait for `status` to show
  it healthy; then the next). A restart sends SIGTERM and the service stops between jobs, so it
  waits at most `TimeoutStopSec` (5 minutes) and then systemd kills it; killing mid-job is safe
  because every stage commits per job or per batch. Database changes are additive and applied
  by `init_store` when a service starts. The pacing change has its own order below.
- Voluntary stop of LinkedIn requests: `sudo systemctl stop jds-collect-linkedin jds-process jds-deliver`.

### The LinkedIn pace and schedule gate

The collector, the processor and the packaging stage of `jds-deliver` (which classifies a
LinkedIn offer link) share one request budget on `linkedin.com`, enforced in
`src/linkedin/pace.py` (the `PaceBudget.slot` choke point every request passes through), and
configured in `config/schedule.json`:

| Setting | Shipped value | Meaning |
|---|---|---|
| `timezone` | `Europe/Rome` | the clock the window and the day boundary use |
| `closed_hours` | `[0, 6]` | no LinkedIn request 00:00-06:00 Rome |
| `daily_caps.weekday` / `.weekend` | `1400` / `800` | shared cap on search + description requests per Rome day |
| `pace.start_spacing_seconds.weekday` / `.weekend` | `75` / `110` | where the steady base spacing starts, in seconds |
| `pace.jitter` | `0.2` | every sleep is jittered +/-20 % |
| `pace.max_spacing_factor` | `2.0` | the daily verdict never loosens past twice the start |
| `pace.tighten.calm_days` | `3` | consecutive calm days that tighten the spacing |
| `pace.tighten.calm_refusal_rate` | `0.02` | a calm day is under 2 % refusals |
| `pace.tighten.loosen_refusal_rate` | `0.10` | a bad day is over 10 % refusals |
| `pace.tighten.step` | `0.10` | one 10 % step per verdict, down or up |
| `pace.tighten.min_day_requests` | `100` | a quieter Rome day gives no verdict |
| `refusals.first_wait_seconds` | `60` | the first refusal waits 60 s |
| `refusals.max_wait_seconds` | `1800` | the doubling wait is capped at 30 min |
| `refusals.hour_refusal_rate` | `0.10` | over 10 % of the last hour refused |
| `refusals.hour_multiplier` | `1.5` | nudges the spacing up by 1.5x |
| `refusals.hour_max_multiplier` | `3.0` | the nudge is capped at 3x the base |

`config/collector.json` sets the per-slice cadence and the page caps:

| Setting | Shipped value | Meaning |
|---|---|---|
| `due_hours` | `12/24/48/24` | tier 1 / tier 2 / tier 3 / tier 4 base cadence, in hours |
| `min_interval_hours` / `max_interval_hours` | `12` / `48` | hard bounds on any adaptive interval |
| `cadence_tolerance_hours` | `6` | a slice counts as past its cadence only beyond its interval plus this tolerance |
| `area_max_pages` | ten countries at `20` | the page cap for the ten large tier-3 countries, a walk that reaches it ends as `area_cap` |
| `refused_pages_to_defer` | `3` | refusals in a row before a walk defers its slice |

Other keys this design adds: `config/processor.json` `description_deadline_hours` (`24`, the
point where descriptions start taking every second turn), `config/deliver.json`
`linkedin_blocked_after_open_hours` (`24`) and `linkedin_blocked_repeat_hours` (`24`, the alert
repeats at most once a day), and `config/sources.json` `spacing_seconds` (`[1, 2]` freehire,
`[2, 5]` Himalayas) and the `throttle` block (`default_retry_after_seconds` 60,
`max_wait_seconds` 300, `run_budget_seconds` 1700).

The steady pace has a floor derived from the cap and the 18 open hours: 18 h / 1400 = 46 s
weekday, 18 h / 800 = 81 s weekend. The base starts at 75 s / 110 s and never goes below the
floor. After three consecutive open days under 2 % refusals it tightens 10 %; any day over
10 % loosens it 10 % (to at most twice the start), and a day under 100 requests gives no
verdict. A refusal honours `Retry-After` or waits 60 s, doubling each consecutive refusal to a
30 min cap, and the first clean response resets it. When refusals pass 10 % of the last hour
the spacing is nudged up 1.5x, at most 3x. Searches go first; a description takes the turn when
no search is waiting, and once the oldest waiting job is over `description_deadline_hours` old
descriptions take every second turn. If a walk is refused three times in a row it stops, keeps
its resume point, and the collector moves to the next due slice. The gate now closes only for
the night window and the daily cap.

The resume timers `jds-resume-freehire.timer` and `jds-resume-himalayas.timer` run
`*-*-* 00..01,05..06,11..23:00/30:00`: every 30 min, outside the 07:00-11:00 delivery window
and at least 60 min clear of the 03:00 and 04:00 daily starts, so a resume run can never hold a
source's lock when its daily run starts.

`config/schedule.json` is read once when a service starts, so a change to a cap, the window or
the pace needs the three LinkedIn users restarted:

```bash
sudo systemctl restart jds-collect-linkedin jds-process jds-deliver
```

Do that restart back to back, not one service per hour: the shared counter starts at its first
restart on the new code, so a service still on the old code issues requests that are neither
gated nor counted.

Read the gate and the pace with `python -m src.services.status`. The gate line is one of:

```
LinkedIn gate: 342/1400 requests today (weekday), window open
LinkedIn gate: 2/800 requests today (weekend), window closed until 06:00 Rome
LinkedIn gate: 1400/1400 requests today (weekday), daily cap reached
```

The pace line shows the base spacing for both windows, the hour nudge, the refusals in a row
and the last success, and adds any wait in Rome time:

```
LinkedIn pace: base 75s weekday / 110s weekend, hour nudge x1.0, 0 refusals in a row, last request: search, last success 2026-10-14 12:00 Rome
LinkedIn pace: base 68s weekday / 110s weekend, hour nudge x1.5, 2 refusals in a row, last request: description, last success 2026-10-14 11:40 Rome, waiting until 12:04:00 Rome
```

To restart the base spacing from config (for example after a mistake):

```bash
psql jds -c "UPDATE pace_budgets SET weekday_spacing_seconds = NULL, weekend_spacing_seconds = NULL, \
  calm_days = 0 WHERE host = 'linkedin.com';"
```

That takes effect on the next request; no restart is needed. To reset today's request counter as
well, add `, requests_today = 0, day = NULL` to the same `SET` list.

The title prefilter (`config/processor.json`, on for all four tiers) drops a title that matches
its tier's `scoring.exclude_keywords`. That list is the four strict role words (Principal, Staff,
VP, Director) plus `10+ years`, `8+ years`, `7+ years`, `C++`, `Rust`, `Java` and `on-site only`;
Senior, Lead and Head of are deliberately not in it. Java, Rust and C++ titles are therefore
filtered too.

### Deploying this change

The general "restart one unit at a time" rule above does not apply here. Stop all three LinkedIn
users before the code moves: the old code spaces requests 2-4 s and writes `next_slot_at` after
each one, so a rolling restart could leave an old processor or deliver setting a 2-4 s slot the
new collector would honour, a short burst in the first minutes. With all three stopped, only new
code writes the row again. `SERVICE_DOWN_AFTER` is 1 h, so the short stop raises no service alert.
The collector's stop normally waits the full `TimeoutStopSec=300` and is then killed; at 75 s per
page a walk outlasts it, which is safe because `resume_start` is committed per page, and it is not
a hang.

1. `sudo systemctl stop jds-collect-linkedin jds-process jds-deliver`.
2. `git pull --ff-only` and `.venv/bin/pip install -r requirements.txt`.
3. `.venv/bin/python -m src.linkedin.collector --sync-slices`: runs `init_store` (adding the new
   columns), applies the new cadences and the 12-48 h clamp through `sync_and_clamp`, prints
   `slices: {... 'clamped': N}` and exits. It makes no LinkedIn request.
4. `psql jds -c "UPDATE pace_budgets SET refusal_times = '{}', refusals_today = (SELECT count(*) FROM pace_events WHERE outcome = 'refused' AND at >= date_trunc('day', now() AT TIME ZONE 'Europe/Rome') AT TIME ZONE 'Europe/Rome') WHERE host = 'linkedin.com';"`.
   The new code reads `refusal_times` as the last hour's refusals while the new `request_times`
   starts empty, so a refusal from the hour before the deploy would count as 100 % of the hour and
   nudge the spacing x1.5 for the first hour. The `refusals_today` part makes the deploy day's
   verdict count its pre-deploy refusals. This needs no new code (`refusal_times` already exists).
5. Copy the four `jds-resume-*` units to `/etc/systemd/system/`;
   `sudo systemd-analyze calendar --iterations=40 '*-*-* 00..01,05..06,11..23:00/30:00'` must list
   only :00 and :30 starts, none from 02:00 to 04:59 (the daily runs at 03:00 and 04:00 plus or
   minus an hour) and none from 07:00 to 10:59; `sudo systemctl daemon-reload`;
   `sudo systemctl enable --now jds-resume-freehire.timer jds-resume-himalayas.timer`.
6. `sudo systemctl start jds-collect-linkedin jds-process jds-deliver`. Each runs `init_store`
   again (idempotent). Check `python -m src.services.status`: the gate open, `LinkedIn pace: base
   75s weekday / 110s weekend, hour nudge x1.0`. Expect one "OK again: collection is healthy"
   Telegram note on the first watchdog run (the old `health:collection` ledger row clears once).
   LinkedIn jobs penalised by old 429s keep their `next_attempt_at` (up to 12 h) once after
   deploy, so the first day's description lag is not misread as a new bug.

### Week one review (design 4.6 C and section 8)

After 1 h, 1 day and 7 days, run these against the job store. Each is a single `psql jds -c`
statement.

Requests per Rome day by kind and outcome:

```sql
SELECT (at AT TIME ZONE 'Europe/Rome')::date AS day, kind, outcome, count(*)
FROM pace_events GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;
```

The longest open-hours gap between consecutive requests (partitioned by Rome day, so the
night gap is not counted):

```sql
SELECT max(gap) FROM (
  SELECT at - lag(at) OVER (PARTITION BY (at AT TIME ZONE 'Europe/Rome')::date ORDER BY at) AS gap
  FROM pace_events WHERE spacing_seconds IS NOT NULL) g;
```

Pages per walk by area, with the caps hit next to each other:

```sql
SELECT s.area, count(*) AS walks,
       count(*) FILTER (WHERE w.stop_reason = 'area_cap') AS area_caps,
       count(*) FILTER (WHERE w.stop_reason = 'cap_hit') AS full_caps,
       avg(w.pages)::numeric(6,1) AS avg_pages
FROM slice_walks w JOIN slices s ON s.id = w.slice_id
WHERE s.source = 'linkedin' AND s.active AND w.walked_at > now() - interval '7 days'
  AND w.walked_at >= (SELECT min(at) FROM pace_events WHERE spacing_seconds IS NOT NULL)
GROUP BY 1 ORDER BY 2 DESC;
```

Active slices whose last walk is older than 48 h plus the 6 h cadence tolerance:

```sql
SELECT role, area, last_walked_at FROM slices
WHERE active AND source = 'linkedin'
  AND (last_walked_at IS NULL OR last_walked_at < now() - (interval '48 hours' + interval '6 hours'))
ORDER BY last_walked_at NULLS FIRST;
```

Description lag, sampled from jobs still in `described` (the stage changes when they are scored):

```sql
SELECT percentile_cont(ARRAY[0.5, 0.9, 0.95]) WITHIN GROUP (ORDER BY stage_changed_at - first_seen_at)
FROM jobs WHERE stage = 'described';
```

Share of delivered LinkedIn jobs that reached the digest within 4 days of posting:

```sql
SELECT avg((j.delivered_at - coalesce(j.posted_at, j.first_seen_at) <= interval '4 days')::int)::numeric(4,3)
FROM jobs j
WHERE j.delivered_at IS NOT NULL
  AND j.first_seen_at >= (SELECT min(at) FROM pace_events WHERE spacing_seconds IS NOT NULL)
  AND EXISTS (SELECT 1 FROM sightings s WHERE s.job_id = j.id AND s.source = 'linkedin');
```

The captain then decides on option B (narrowing LinkedIn in Europe). It is not built.

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
