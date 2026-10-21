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
| `jds-watchdog.timer` | every 10 minutes: alerts when a service or the store is down, or a fallback model answers a stage | Telegram, Postgres |
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
5. Create the slices and check the count (127: four roles in 33 areas, less the five `skip_slices` pairs):
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
  LinkedIn request has succeeded for 24 h of open time, a fallback model is answering a stage's
  OpenRouter calls, or Postgres is down.
- Logs: `journalctl -u jds-process --since today`. A refusal shows up as a
  `[pace] LinkedIn refused a ... request ... waiting Ns (refusal K in a row)` line, and the
  `status` pace line shows the refusals in a row and any wait (`waiting until ... Rome`).
  There is no global rest state and no automatic slowing: a refusal is a short capped wait the
  pace applies before the next request, so the system never shuts itself off for hours.
- Language-model use: the day summary shows OpenRouter requests against 1,000. It turns
  `HIGH` from 800. A budget that prioritises tier 1 is built only if usage exceeds 800 a day
  on three days in a fortnight (spec section 6.5).
- Fallback-model guard: OpenRouter answers a request from the next model in the fallback chain
  with an ordinary 200, so a broken primary leaves no error behind. `python -m src.services.status`
  prints, per scoring/verification/tailoring stage, today's OpenRouter calls, the share answered
  by the stage's pinned primary and the top answering model. When fallbacks answer more than
  `share` (20%) of a stage's answered calls, from at least `min_calls` (5) answered calls, the
  watchdog sends one Telegram alert per stage per Rome day naming the stage, the answering
  models, the primary and the counts. `config/llm_fallback.json` holds both numbers, and
  `enabled: false` switches the alert off (the status line stays). A call that never answered is
  counted as unanswered and does not trigger it: a dead stage is a stage failure, not a fallback
  answer.
- Remote-check verdicts (2026-10-18): each tier digest now has up to three sections, "Confirmed
  full-remote", "Remote, eligibility to check" (a short advice line says what to check) and
  "Work mode not stated". Only the last one is withheld from Telegram strong-match alerts; it
  still appears in the daily digest and the vault. `rejected.md` still carries every rejection,
  now including remote-only-as-a-perk. In tiers 3 and 4 the rule pre-pass settles eligibility
  only for an explicit "Remote (UK)" or "Remote - Italy", so most remote-phrased jobs there
  cost one verification call. Inspect the mix with
  `psql jds -c "SELECT remote_verdict, count(*) FROM jobs WHERE tier IN (1,3,4) AND first_seen_at > now() - interval '7 days' GROUP BY 1;"`.
- Remote check (2026-10-21): occasional presence - an onboarding period, team events,
  offsites, team sprints, meetups, a few trips a year, occasional client travel - is not
  hybrid, in the free rules and in the model prompt; technology wording ("hybrid cloud",
  "hybrid environments") is not a work mode either. A source `remote`/`remote_worldwide`
  hint with a silent description, or a model answer of `not_stated`, is `advised` with the advice "the source marks it remote"
  (in a tier that needs eligibility: "... - check eligibility"), shown under the existing
  "Remote, eligibility to check" heading. A job the source marks remote is never stored
  `rejected` when the model's only evidence is occasional presence (both the model's reason
  and the text must carry such a phrase, and a regular weekly cadence still rejects). An
  explicit "fully remote"/"100% remote" in the title or text beats the source's hybrid
  shortcut; the shortcut is dropped and the model decides, in every tier, so a loose "fully
  remote" phrase cannot turn a source hybrid job into a rule-level `confirmed` (the only
  verdict that is auto-tailored). Six switches, each default on, all in
  `config/processor.json` - set one to `false` and restart `jds-process`, no deploy needed:
  `remote_rules.occasional_presence`, `remote_rules.cloud_hybrid`,
  `remote_hint.advised_when_silent`, `remote_hint.never_reject_on_occasional`,
  `structured_remote.explicit_remote_wins`, `remote_verifier.occasional_presence_prompt`.
  Turning one off restores the previous behaviour for that rule (with
  `occasional_presence=false`, the prompt renders exactly as before). The disabled
  `merged_call` path builds its own prompt, so it gets neither the guidance nor the
  post-model guard, and the retiring `main.py` pipeline (section 8) calls `verify_offers`
  directly, so it gets the new prompt guidance with no switch of its own.
- Deploying new code: `git pull --ff-only && .venv/bin/pip install -r requirements.txt`, then
  restart one unit at a time (`sudo systemctl restart jds-process`; wait for `status` to show
  it healthy; then the next). A restart sends SIGTERM and the service stops between jobs, so it
  waits at most `TimeoutStopSec` (5 minutes) and then systemd kills it; killing mid-job is safe
  because every stage commits per job or per batch. Database changes are applied
  by `init_store` when a service starts. They are additive except for one migration in the
  2026-10-18 remote-verdict release: the first service start after that pull drops and
  recreates `jobs_remote_verdict_chk` so `advised`/`not_stated` can be stored (a validation
  scan over the jobs table, a few seconds at production size) and adds `remote_advice`. The
  fingerprint then matches, so later starts do no DDL. A release that edits the DDL list
  re-runs the whole list once on its first start, because the fingerprint hashes that list (a
  release that does not edit it re-runs nothing): the 2026-10-21 description-order release
  adds `jobs.description_class` and re-runs that same constraint drop and re-add, so it holds
  the jobs table for a few seconds too. Restart
  `jds-process` first and wait for `python -m src.services.status` to show it healthy, then
  restart the other units: only the first service runs the DDL, and the rest start against a
  matching fingerprint. A code rollback causes the same one-time scan. The pacing change has
  its own order below.
- Turning the description order off without a deploy: set `"enabled": false` inside
  `description_order` in `config/processor.json` and `sudo systemctl restart jds-process`. Every
  pick is then a floor pick, i.e. today's two queries. The order still classifies waiting jobs
  and still advances the per-tier counters; it changes the order, nothing else. The file is
  tracked: commit or reset a server-side edit before the next `git pull --ff-only`. The first
  tick after a deploy adds a class to up to `description_order.backfill_per_tick` waiting jobs
  before it picks, so a 3,000-row backlog is covered in about six ticks (12 min at the 120 s
  idle cadence); `python -m src.services.status` shows the queue split by class once they are.
- Rolling back past the 2026-10-18 remote-verdict release is not a plain code rollback: older
  code cannot read `advised` or `not_stated`, and `jds-deliver` would fail on the first such
  row. Stop `jds-process` and `jds-deliver`, run
  `psql jds -c "UPDATE jobs SET remote_verdict = 'unconfirmed' WHERE remote_verdict IN ('advised','not_stated');"`,
  then check out the older commit and start the units again. The `remote_advice` column can
  stay; older code ignores it.
- Voluntary stop of LinkedIn requests: `sudo systemctl stop jds-collect-linkedin jds-process jds-deliver`.

### The LinkedIn pace and schedule gate

The collector, the processor and the packaging stage of `jds-deliver` (which classifies a
LinkedIn offer link) share one request budget on `linkedin.com`, enforced in
`src/linkedin/pace.py` (the `PaceBudget.slot` choke point every request passes through), and
configured in `config/schedule.json`:

| Setting | Shipped value | Meaning |
|---|---|---|
| `timezone` | `Europe/Rome` | the clock the window and the day boundary use |
| `closed_hours` | `null` | no closed window: LinkedIn runs around the clock (set `[0, 6]` to restore the 00:00-06:00 Rome pause) |
| `pace.start_spacing_seconds.weekday` / `.weekend` | `75` / `110` | the fixed base spacing, in seconds |
| `pace.jitter` | `0.2` | every sleep is jittered +/-20 % |
| `pace.description_turn_share` | `0.6667` | descriptions get this share of the turns when a search also waits (realised as a run of `round(share/(1-share))` = 2) |
| `refusals.first_wait_seconds` | `60` | the first refusal waits 60 s |
| `refusals.max_wait_seconds` | `1800` | the doubling wait is capped at 30 min |

There is no daily request cap: an old or rolled-back `config/schedule.json` that still carries
the removed `daily_caps` block loads anyway, the block is ignored and a `[schedule]` line names
it, so a stale config never stops the service. Delete it when you next edit the file.

`config/collector.json` sets the per-slice cadence and the page caps:

| Setting | Shipped value | Meaning |
|---|---|---|
| `due_hours` | `12/24/48/24` | tier 1 / tier 2 / tier 3 / tier 4 base cadence, in hours |
| `min_interval_hours` / `max_interval_hours` | `12` / `48` | hard bounds on any adaptive interval |
| `cadence_tolerance_hours` | `6` | a slice counts as past its cadence only beyond its interval plus this tolerance |
| `area_max_pages` | ten countries at `20` | the page cap for the ten large tier-3 countries, a walk that reaches it ends as `area_cap` |
| `refused_pages_to_defer` | `3` | refusals in a row before a walk defers its slice |

Other keys this design adds: `config/processor.json` `description_deadline_hours` (`24`, the
point where an overdue description may spend more than one tick) and
`description_urgent_after_hours` (`60`, the age past which a waiting job is eligible for the
floor pick: it is guaranteed one pick in `description_order.quota_one_in` per tier ahead of the
class order, not absolute priority), `config/deliver.json`
`linkedin_blocked_after_open_hours` (`24`) and `linkedin_blocked_repeat_hours` (`24`, the alert
repeats at most once a day), `config/llm_fallback.json` `share` (`0.2`), `min_calls` (`5`) and
`enabled` (the fallback alert's off switch), and `config/sources.json` `spacing_seconds` (`[1, 2]`
freehire, `[2, 5]` Himalayas) and the `throttle` block (`default_retry_after_seconds` 60,
`max_wait_seconds` 300, `run_budget_seconds` 1700).

`config/processor.json` `description_order` decides which waiting description is worth
fetching next (see "Which description goes next" below). Every key, with the shipped value:

| Setting | Shipped value | Meaning |
|---|---|---|
| `description_order.enabled` | `true` | the whole order; `false` (edit + restart `jds-process`, no deploy) reproduces the pre-order behaviour exactly, one plain oldest-first pick per fetch |
| `description_order.quota_one_in` | `4` | one pick in four, per tier, is the plain oldest-first pick (the 60-hour floor and the rotation); `1` makes every pick one |
| `description_order.backfill_per_tick` | `500` | waiting jobs given a priority class per processor tick when they have none (a 3,000-row backlog takes six ticks) |
| `description_order.ai_title_keywords` | `AI, GenAI, generative AI, artificial intelligence, machine learning, ML, MLOps, deep learning, LLM, LLMs, NLP, data scientist(s), data science, computer vision` | whole-word title keywords that make a title "AI/ML/DS"; matched case-insensitively on word boundaries |
| `description_order.senior_title_keywords` | `Senior, Sr, Lead, Principal, Staff, Head, Director, VP, Vice President, Chief, Manager` | whole-word seniority keywords that demote an AI title to the `other` classes |
| `description_order.classes` | `default` (tiers 1, 3, 4): `ai_country`, `ai_city`, `other_country`, `other_city`; `"2"`: `ai`, `other` | the class order per tier, best first; tier 2 omits `location`, since it accepts any work mode |
| `description_pick_count:<tier>` (kv_state) | one row per tier | how many picks that tier has made, i.e. the phase of its floor share; deleting the rows only resets the phase |

The block is validated when the config loads, so edit only the `enabled` value (or a value this
table documents); a malformed `description_order` block stops every service that loads the
processor config (`jds-process`, `jds-deliver`, the experiment CLI) at start, and the operator
view prints one "processor config is unreadable" line instead of its queue block (the rest of
`python -m src.services.status` keeps working). Changing `classes`
or either keyword list changes the ranks, so run
`psql jds -c "UPDATE jobs SET description_class = NULL WHERE stage = 'seen' AND tier IS NOT NULL AND description_status IN ('pending', 'rate_limited');"`
and let the next ticks reclassify the backlog at `description_order.backfill_per_tick`.

The base is a constant 75 s / 110 s, jittered +/-20 %, and nothing clamps it: there is no
request cap, so open hours do not set a floor and a finished day changes nothing (the
2026-10-18 refusal study removed the hour nudge and the day verdict, which only cut
throughput). The shared counter still records each request per Rome calendar day, but it is
operator information: it never closes the gate. A refusal honours
`Retry-After` or waits 60 s, doubling each consecutive refusal to a 30 min cap, and the first
clean response resets it; the spacing around it does not move. When both a search and a
description are waiting, descriptions take two of every three turns (`pace.description_turn_share`,
realised as a run of two); a lone waiting kind always takes the turn. Once the oldest waiting
job is over `description_deadline_hours` old, an overdue description that the share says takes
the next turn may spend more than one processor tick waiting for it. If a walk is refused three
times in a row it stops, keeps its resume point, and the collector moves to the next due slice.
The gate closes only for a configured `closed_hours` window: there is no night window as
shipped and no daily cap.

Which description goes next is decided by `src/processor/describe.py` `_next_job`, and it does
not sweep the tiers in order. The head tier is the one whose last fetch attempt is oldest, and
its own pick counter decides the kind of the pick:

- one pick in `description_order.quota_one_in` per tier is the **floor pick**, exactly today's
  queries: a job past `description_urgent_after_hours` (60 h) first, whatever its tier, oldest
  first, else that tier's oldest job. The 60-hour rule is therefore a guaranteed share of the
  fetches, not absolute priority.
- every other pick is a **class pick**: the same tier rotation (the tier whose last attempt is
  oldest goes first, ties to tier 1), then the best priority class inside that tier
  (`ai_country` > `ai_city` > `other_country` > `other_city` for tiers 1/3/4; `ai` > `other` for
  tier 2), then the oldest job, ties on `note_no`. A class pick has no urgent cutoff, so a fresh
  high-value job can precede an aged low-value one.

The class is written when the cheap checks assign the tier and read as one smallint column
(`jobs.description_class`); the rotation and the floor share are `kv_state` rows per tier
(`description_turn:<tier>`, `description_pick_count:<tier>`), not new timestamps. Deleting the
turn rows restarts the rotation from tier 1, deleting the pick rows only resets the share's
phase. The pace, the number of requests and the set of jobs fetched are unaffected, only the
order. The measured effect: at the 2:1 inflow/capacity of 2026-10-20, the most valuable jobs
reach a description at a median of 3 h and a p90 of 13 h (max 26 h) in every tier, against a p90
of 193 h in the old order; low-value jobs are still fetched but their tail waits about 10 days
at day 12 (median 167 h, p90 229 h, max 244 h) and ages out at the 30-day expiry, because
description capacity is about half of the inflow. `description_order.quota_one_in` is the dial
that trades high-value latency against that tail (1 = pure FIFO, 4 = shipped); more description
capacity is a pace decision outside this change.

A flat 110 s over 24 h would be about 785 requests; with the +/-20 % jitter the mean stays at
110 s, so the weekend still does about 785 and the weekday about 1,152 at 75 s. Nothing stops
the evening: the only limit on volume is the spacing itself.

The resume timers `jds-resume-freehire.timer` and `jds-resume-himalayas.timer` run
`*-*-* 00..01,05..06,11..23:00/30:00`: every 30 min, outside the 07:00-11:00 delivery window
and at least 60 min clear of the 03:00 and 04:00 daily starts, so a resume run can never hold a
source's lock when its daily run starts.

`config/schedule.json` is read once when a service starts, so a change to the closed
window or the pace needs the three LinkedIn users restarted:

```bash
sudo systemctl restart jds-collect-linkedin jds-process jds-deliver
```

Do that restart back to back, not one service per hour: the shared counter starts at its first
restart on the new code, so a service still on the old code writes requests the new code never
counts.

Read the gate and the pace with `python -m src.services.status`. The gate line reads:

```
LinkedIn gate: 342 requests today (weekday), window open
```

With a restored `closed_hours` the closed line appears as
`LinkedIn gate: 2 requests today (weekend), window closed until 06:00 Rome` during the pause.
The count resets at Rome midnight; it is reported, never enforced, and the line has no cap.

The pace line shows the fixed spacing for both windows, the day's refusals, the refusals in a
row and the last success, and adds any wait in Rome time:

```
LinkedIn pace: fixed 75s weekday / 110s weekend, 0/342 refused today, 0 refusals in a row, last request: search, last success 2026-10-14 12:00 Rome
LinkedIn pace: fixed 75s weekday / 110s weekend, 2/350 refused today, 2 refusals in a row, last request: description, last success 2026-10-14 11:40 Rome, waiting until 12:04:00 Rome
```

The stored `weekday_spacing_seconds`, `weekend_spacing_seconds` and `nudge` columns from the
old adaptive pace are ignored by the fixed code, so the live row's 99 s / 133 s base and x3
nudge cannot slow the deploy: it runs at 75 s / 110 s from the first request, no manual step
needed. To clear a stuck turn share (give descriptions the next turn) or reset today's counter:

```bash
psql jds -c "UPDATE pace_budgets SET description_run = 0, requests_today = 0, day = NULL \
  WHERE host = 'linkedin.com';"
```

That takes effect on the next request; no restart is needed. The old `weekday_spacing_seconds`,
`weekend_spacing_seconds`, `calm_days`, `nudge` and `nudged_at` columns may be left as they are.

### The fixed-pace tripwire (2026-10-18 refusal study)

The study found the refusal rate did not depend on the pace between 3 s and 390 s, so the pace is
fixed at 75 s / 110 s and accepts the ~16 % refusal rate. The one thing the study could not see is
a long run above the 29 requests/hour it observed, so watch the first days. Revert to the cautious
91 s / 133 s if any of these happens:

- the daily refusal rate goes above about 35 %;
- LinkedIn returns a status code the pace does not already know;
- a full day of consecutive refusals.

Daily refusal rate, per Rome day:

```sql
SELECT (at AT TIME ZONE 'Europe/Rome')::date AS day,
       count(*) FILTER (WHERE outcome = 'refused') AS refused, count(*) AS requests,
       round(100.0 * count(*) FILTER (WHERE outcome = 'refused') / count(*), 1) AS pct
FROM pace_events GROUP BY 1 ORDER BY 1 DESC;
```

The exact pace revert, on the VPS:

1. Edit `config/schedule.json`: `pace.start_spacing_seconds` to `{"weekday": 91, "weekend": 133}`.
2. `sudo systemctl restart jds-collect-linkedin jds-process jds-deliver` back to back.
3. `python -m src.services.status` must show `LinkedIn pace: fixed 91s weekday / 133s weekend`.

If the extra night requests are the problem, also restore the closed window in the same edit:

1. Edit `config/schedule.json`: `closed_hours` to `[0, 6]` (no other change).
2. `sudo systemctl restart jds-collect-linkedin jds-process jds-deliver` back to back.
3. `python -m src.services.status` at night must show `window closed until 06:00 Rome`.

No database change and no code change is needed: the pace and the closed window are read from
config at service start.

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
4. No pace-state reset is needed: the fixed code reads the base spacing from
   `config/schedule.json` and ignores the stored `weekday_spacing_seconds`,
   `weekend_spacing_seconds` and `nudge`, so the live row's old base and nudge cannot slow it.
   `init_store` adds `description_run`.
5. Copy the four `jds-resume-*` units to `/etc/systemd/system/`;
   `sudo systemd-analyze calendar --iterations=40 '*-*-* 00..01,05..06,11..23:00/30:00'` must list
   only :00 and :30 starts, none from 02:00 to 04:59 (the daily runs at 03:00 and 04:00 plus or
   minus an hour) and none from 07:00 to 10:59; `sudo systemctl daemon-reload`;
   `sudo systemctl enable --now jds-resume-freehire.timer jds-resume-himalayas.timer`.
6. `sudo systemctl start jds-collect-linkedin jds-process jds-deliver`. Each runs `init_store`
   again (idempotent). Check `python -m src.services.status`: the gate open, `LinkedIn pace: fixed
   75s weekday / 110s weekend`. Expect one "OK again: collection is healthy"
   Telegram note on the first watchdog run (the old `health:collection` ledger row clears once).
   LinkedIn jobs penalised by old 429s keep their `next_attempt_at` (up to 12 h) once after
   deploy, so the first day's description lag is not misread as a new bug. `init_store` also
   builds `jobs_description_queue_idx`, the partial index the new description pick reads; on a
   300k-row store it builds in well under a second.

### Week one review (design 4.6 C and section 8)

After 1 h, 1 day and 7 days, run these against the job store. Each is a single `psql jds -c`
statement.

Requests per Rome day by kind and outcome:

```sql
SELECT (at AT TIME ZONE 'Europe/Rome')::date AS day, kind, outcome, count(*)
FROM pace_events GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;
```

The longest gap between consecutive requests, across midnight too (there is no nightly pause):

```sql
SELECT max(gap) FROM (
  SELECT at - lag(at) OVER (ORDER BY at) AS gap
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
