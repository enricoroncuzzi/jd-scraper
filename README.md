# jd-scraper - grounded LLM pipeline for AI/ML job hunting

> A production job-hunt pipeline that collects AI/ML postings across four regions, verifies remote eligibility and scores fit with structured LLM output, keeps everything in a server-side Postgres job store (archived to Neon), and tailors a CV, cover letter, and recruiter message per posting, with every CV bullet taken from the real CV.

Built solo, test-driven, running unattended in production as systemd services.

## The interesting part: grounded generation, enforced in code

The hard problem here isn't collecting jobs, it's making an LLM produce text a hiring manager can trust. Three design choices do that:

- **Structured output everywhere an LLM touches the pipeline.** Scoring (`src/scorer.py`), remote-eligibility verification (`src/remote_verifier.py`), and CV tailoring (`src/tailor/generate.py`) each define a Pydantic schema and parse the model's response into it. No regex on free text, no brittle string parsing.
- **Anti-hallucination enforced at runtime, not by prompt wording.** `src/tailor/cv_master.py`'s `assemble()` never asks the LLM to write CV content: every bullet is included in canonical order, and only the skill groups are reordered, verbatim, from a hand-written canonical CV. The LLM only writes the cover-letter hook/bridge and the recruiter message. `src/tailor/validate.py` then byte-matches the assembled CV against six required metrics (e.g. `"94.1%"`, `"10.5M"`) and halts the run if one is missing. The cover letter is checked for banned phrases, length, a concrete bridge, and a first-person voice. Hook claims are written to `CLAIMS_REVIEW.txt` for a person to confirm, rather than trusting the model to have been careful.
- **A server-side Postgres job store** (`src/jobstore/schema.py`) that is the only state the collector, processor and delivery services share, with every stage committing per job or per batch so a restart resumes where it stopped. Finished jobs are copied in batches to a Neon archive (`src/delivery/archive.py`) that holds the scored, full-text corpus, the kind of ground-truth data set an eval or retrieval layer would need, growing daily from real traffic rather than a scraped-once snapshot.

## How it runs today: a job store and long-running services

The production path is no longer a once-a-day cron run. It is long-running services around a PostgreSQL job store on the server. Services never call each other; the store is their only shared state. The install, switch-over and rollback procedure, including the systemd units in `deploy/systemd/`, lives in [`deploy/RUNBOOK.md`](deploy/RUNBOOK.md).

- **LinkedIn collector** (`jds-collect-linkedin`, `src/linkedin/collector.py`) - walks "slices", one per role and area, built from the four tier configs by `src/linkedin/catalog.py` into the `slices` table, and records every card as a sighting. It never fetches a description and never judges a job.
- **Daily source collectors** (`jds-collect-freehire`, `jds-collect-himalayas`, `src/sources/`) - one oneshot pull a day each (03:00 and 04:00 server time) from freehire.me and Himalayas, whose public JSON APIs return full descriptions inline, plus `jds-resume-freehire.timer` and `jds-resume-himalayas.timer` every 30 minutes to finish an interrupted walk. Configured in `config/sources.json`.
- **Processor** (`jds-process`, `src/processor/runner.py`) - the stage machine: expire stale jobs, cheap no-network checks (tier assignment, cross-source merge, title pre-filter), the remote check (a deterministic rule pre-pass, then the LLM for what the rules will not settle; off for tier 2, which accepts any work mode), LLM scoring, then LinkedIn description fetches through the shared pace, with the language check applied as soon as a job has a description. Batch sizes, backoff and tick limits are in `config/processor.json`; the score cutoff, keywords and candidate profile come from the tier configs.
- **Delivery** (`jds-deliver`, `src/delivery/`) - immediate Telegram alerts for jobs at or above the cutoff (not for a job whose work mode is not stated), the daily delivery at 08:00 Europe/Rome (one digest per tier plus a day summary with collection, requests and new jobs per source, budget and service health), auto-tailoring of above-cutoff confirmed jobs (a package per job; the Telegram notice stays off while `autoapply.dry_run` is true, as shipped), and the periodic Neon archive copy. Each day also lands in `OUTPUT_PATH/<date>/tier<N>/` as one note per job, a ranked `digest.md` whose remote-checked sections are "Confirmed full-remote", "Remote, eligibility to check" and "Work mode not stated", and a `rejected.md` audit trail of what the remote check screened out and why. A tailored package lands under the day folder whose digest lists the job (`<date>/tier<N>/tailored/`), so a job alerted after that day's 08:00 delivery is packaged under the next morning's folder, the one the captain opens, rather than the alert day's. Timing and expectations are in `config/deliver.json`.
- **Watchdog** (`jds-watchdog.timer`, `src/services/watchdog.py`) - every ten minutes, messages Telegram when a service has been silent for over an hour, a daily source collector has missed its run by more than three hours (or ended in error), no LinkedIn request has succeeded for 24 h of open time (at most one alert a day), a fallback model is answering a stage's OpenRouter calls (one alert per stage per Rome day), or the job store is unreachable.
- **Nightly backup** (`jds-backup.timer`, `deploy/backup-jobstore.sh`) - a `pg_dump` of the job store, keeping the seven newest dumps.

`LinkedIn slices -> sightings -> cheap checks -> description -> language -> remote check -> score -> delivery (digest + alerts) -> [tailor] -> OpenRouter generation + validation gate -> PDF -> Neon archive`

The `jobs` table is the stage machine: `seen -> described -> filtered_out | scored -> delivered | expired`. `sightings` records every source that saw a posting, `job_applications` the packaged ones, and `service_heartbeats`/`pace_events`/`source_requests`/`llm_calls` the operational record. The pre-redesign entrypoints (`main.py`, `orchestrator.py`, `src/scraper.py`) still ship during the switch-over window; `deploy/RUNBOOK.md` covers their retirement.

## The LinkedIn schedule (schedule C)

Every LinkedIn request the collector, the processor and the packaging stage make passes one choke point, `src/linkedin/pace.py`, so they cannot exceed a shared budget together or have two requests in flight. `config/schedule.json` sets the policy:

- no quiet hours: LinkedIn runs around the clock (`closed_hours` is `null` in `config/schedule.json`);
- no daily request cap: the spacing (and, when enabled, the ladder's level) is the only thing that limits volume, and the day's count is operator information (`python -m src.services.status`);
- one base spacing of 75 s on every day of the week, jittered +/-20 %; the weekend is no different (the 2026-10-18 refusal study found the refusal rate did not depend on the gap, while the old nudge and day verdict cut throughput to about a quarter);
- an adaptive ladder, shipped on (`adaptive.enabled: true`), that learns from the refusal rate: eight levels, 75, 50, 37.5, 30, 25, 18.75, 15 and 10.7 s (1x to 7x), with `adaptive.max_level` as the ceiling (8 as shipped, so 4x or 3x is a config edit); a trailing-20 protect rule that drops one level at 30 % refused and two at 50 % or four in a row, a slow climb only on a clean answer after 200 requests with a cautious upper refusal estimate under 12 %, a refused share in the recent window below the protect threshold, no refusal in the last four requests, and more successes per hour than the level below, per-level memory from 2 h up to 24 h that halves after each clean day, demand-aware easing, and the free OpenRouter limit as a reason to stop climbing; above 3x a climb needs 600 requests at the level, there is at most one such climb per Rome day, a level above 3x is dropped to 3x the moment fewer than 250 descriptions wait, a rolling 24 h request ceiling of 5,000 holds the ladder at 3x (so on a full queue the ladder cycles 3x to 5x and reaches 7x only if that ceiling is raised, which trades the free LLM budget for speed), no climb above 4x happens once 75 % of the OpenRouter stop line is spent, and any protect drop or brake from above 3x lands at 3x with a 24 h ban on climbing back; the off switch returns to exactly 75 s;
- a real block (403, 999, CAPTCHA, three dropped connections in 10 minutes, or the 30-minute refusal wait) brakes to 150 s with climbing frozen; if it keeps recurring for an hour, LinkedIn pauses for the rest of the Rome day and the watchdog alerts once through the existing health ledger;
- a refusal is a short capped wait (60 s doubling to 30 min, or `Retry-After`), never a shutdown and never a change of pace while the ladder is off;
- descriptions take two of every three turns when a search is also waiting (`pace.description_turn_share`), with a 24 h guard that lets an overdue description spend more than one processor tick, and the one LinkedIn request a CV package needs takes the next turn (the `packaging` kind), so packaging a LinkedIn-hosted job cannot lose its turn to the describer;
- every area walked within its cadence, with a 6 h tolerance on the 48 h bound; the description queue is ordered by measured value across all tiers, not by tier rotation. A value pick takes the best class first anywhere in the queue (`description_order.value_rank` in `config/processor.json`: `ai_country` 19.2 %, `ai` 17.6 %, `ai_city` 4.2 %, `other_country` 3.3 %, `other` 1.2 %, `other_city` 0.68 % of one description turning into a `score >= 7`), then the oldest job; one pick in eight is the head tier's floor pick, which keeps the 60-hour rule (`description_urgent_after_hours`) as a guaranteed share rather than absolute priority and otherwise takes that tier's oldest AI-title job, so no tier's important work starves. The 60-hour rule leaves out the jobs the 96 h age-out covers (tiers 3 and 4, non-AI classes), so they never take a floor turn. Measured with the shipped settings the most valuable jobs reach a description in hours even while the queue is over capacity (p90 13 h, max 26 h, against 193 h p90 for the same jobs in the old order), and the value order is worth about +20 % `score >= 7` a day at the same request count. The accepted consequence is that the lowest-value postings mostly wait, then age out at 96 h in tiers 3 and 4 (`age_out` in `config/processor.json`, reversible) instead of holding the queue. `python -m src.services.status` shows the waiting queue split by class and value rank.

Slices carry their own cadence (`config/collector.json`, `due_hours` 12/24/48/24 plus per-area overrides, bounded by `min_interval_hours`/`max_interval_hours`). The ten large tier-3 countries are walked at most 20 pages per walk. freehire and Himalayas use the same patient approach: jittered 1-2 s / 2-5 s spacing and a bounded 429 wait, with the 30-minute resume timers for an unfinished walk. The live gate, budget and pace are in `python -m src.services.status`, and `deploy/RUNBOOK.md` carries the config tables and the deploy order.

## LLM stages and caps

Remote verification runs on OpenRouter (`nvidia/nemotron-3.5-super-vl:free`, with a three-model native fallback chain) and falls back to Groq `openai/gpt-oss-20b`. Verification is capped at 4/10 of the account-wide OpenRouter allowance (400 of 1,000 requests), and only that stage is capped. It is a filter, not a gate: nothing it decides drops a job silently, and a stage failure is deferred and retried instead of being stored as a verdict.

The check is two steps (design decision 2026-10-18, after the full-remote scout):

1. **A free deterministic pre-pass** (`src/remote_rules.py`) reads the description, title, location and the source's own work mode. Confident hybrid/on-site phrases in English, Italian, Spanish, French and German reject; confident fully-remote phrases ("fully remote", "remote-first", "100% teletrabajo", "vollständig remote") confirm the work mode; in tiers 3/4 the pre-pass settles eligibility only for an explicit "Remote (UK)" (advised) or "Remote - Italy" (confirmed), and every other remote posting there goes to the model, because residency wording ("based in", "only", "citizens") is as often about the company as about the candidate; a description with no work-mode vocabulary at all is `not_stated` without a model call. Whenever the result would be `not_stated`, rule or model, and the source's own field marks the job `remote`/`remote_worldwide`, it is stored `advised` with an advice line saying the source marks it remote (a rule that already decided keeps its result). Occasional presence is not hybrid: an onboarding period, team events, offsites, team sprints, meetups and a few trips a year do not reject a job, and technology wording ("hybrid cloud", "hybrid environments") is ignored. Hybrid/on-site from freehire/Himalayas is a shortcut (99.8% agreement on 420 decided jobs), unless the title or text says the role is fully remote, in which case the shortcut is dropped and the model decides; the source's *confirm* cases (87.3% and 66.7%) are not used as shortcuts. Borderline wording ("smart working", "hybrid or remote", a negated phrase) always goes to the model.
2. **One structured model answer** with `work_mode` (remote/hybrid/onsite/flexible/not_stated), `eligibility` (open/restricted/not_stated) and a quote copied from the posting. `open` means worldwide, EU/EEA, Europe or EMEA, or a stated restriction that includes Italy; `restricted` means one that excludes Italy; plain "Remote" is `advised` in tiers 3/4. The model reads the whole description instead of the old 1,000-character keyword excerpt, bounded at 6,000 characters and, for a full 8-offer batch, about 3,000 characters each so the request stays inside Groq's free per-minute token limit. A quote that is not found in the title, location or description is treated as `not_stated`, never as a rejection. `remote-first` counts as fully remote. The prompt says occasional presence (onboarding, team events, offsites, sprints, meetups, a few trips a year, client travel) is not hybrid, and a job the source marks `remote` is never stored `rejected` when the model's only evidence is such a phrase.

The verdict vocabulary is in `src/remote_verdicts.py`: `confirmed` (fully remote, workable from Italy), `advised` (fully remote but limited to another country, with no stated country restriction in tiers 3/4, or the source marks it remote while the text is silent - **never rejected**, delivered and alerted with a short advice line), `not_stated` (the text never says; **kept in the digest in its own section and never alerted on Telegram**), `rejected` (hybrid, on-site, or remote only as an occasional perk), and `not_checked` (tier 2). The legacy `unconfirmed` value is still accepted and reads as `not_stated`. Only `confirmed` and `not_checked` are auto-tailored.

Scoring uses its own pins (`nvidia/nemotron-3-ultra-550b-a55b:free` primary) and its own fallback chain, kept independent of verification's. Daily provider limits live in `config/llm_limits.json` (Groq 200,000 tokens; OpenRouter 1,000 free-model requests per account), and the processor stops the LLM stages at `llm_stop_at_requests` (950 today) to leave headroom for tailoring. There is no paid model and no local model: both were weighed in the scout and declined (D4/D5). OpenRouter's native fallback answers with a 200 from a different model, so `llm_calls.response_model` is the only trace of a broken primary: `python -m src.services.status` prints, per stage, today's OpenRouter calls, the share answered by the pinned primary and the top answering model, and the watchdog sends one Telegram alert per stage per Rome day when fallbacks answer more than `share` (20%) of a stage's answered calls, from `min_calls` (5) answered calls (`config/llm_fallback.json`; `enabled: false` switches the alert off).

## CV tailoring, working today

`tailor.py <job>` calls OpenRouter (`nvidia/nemotron-3-ultra-550b-a55b:free`) to reorder skill groups from a canonical CV for the specific job description. Every bullet is included, in canonical order. The model writes only the cover letter's hook/bridge and the recruiter message as free text. Everything else in the CV is copied byte-for-byte. The validation gate in `src/tailor/validate.py` aborts the run if a required metric is missing, or if the cover letter fails its checks for banned phrases, length, a concrete bridge, and a first-person voice (a hook or bridge that talks about Enrico in the third person is a violation). Hook claims are written to `CLAIMS_REVIEW.txt` for a person to confirm. Headless Chromium then renders CV, cover letter, and recruiter message to PDF, reproducing the original template offline, and the CV and the cover letter must each stay a single page. The cover letter's letterhead, fixed text and proof pool are read from a hand-written `CL_master.md` next to the CV master, so the letter has a source the owner can read and edit; a broken or missing master stops packaging up front. The package is one directory per offer. Each package also carries `TAILOR_TRACE.json` (written on success and on a gate stop): the sha256 of the CV master, letter master and CSS it was made with, the prompt version, the model asked and the model that answered, the CV's fitted font size, whether the server (`auto`) or a Mac re-tailor (`manual`) made it, the selection (skill order, proof, hook and bridge) and the letter's paragraphs by kind. It holds no paths and no contact details.

The delivery service runs the same engine automatically for above-cutoff jobs: it writes the package and, once `autoapply.dry_run` is switched off in the tier config, sends a Telegram notice. It is a draft-and-notify system with no code path that submits an application. Each digest entry also has ⭐ golden and 👎 not for me links. After a confirmation dialog they record the offer in the `offer_labels` table (one label per posting, re-clicking is a no-op), building a labelled set for evals. `python label.py --list golden` prints it and `python label.py --remove golden <link-or-note>` undoes a label. Each label is linked to the archived job it is about, and listing fills that link in for labels recorded without it, so the title, company and score still show when the sighting that made the posting canonical came from another board. The handler is installed by `scripts/install-tailor-app.command`.

## Tech stack

`Python` - `OpenRouter` & `Groq` LLM APIs - `Pydantic` - `PostgreSQL` (server job store + Neon archive) - `systemd` - `Playwright` (headless Chromium) - `LangChain` (thin scoring adapter) - `BeautifulSoup` (source parsing) - `pytest` (TDD)

## Engineering notes

- **Test-driven throughout**: `.venv/bin/python -m pytest tests/`, every module built red to green. Count them with `pytest tests/ --collect-only -q`; the number moves.
- **Structured LLM I/O everywhere**: Pydantic schemas for scoring, remote verification, and tailoring, no string-parsed output.
- **No silent loss**: every stage transition is one `UPDATE` in the caller's transaction, and each stage commits per job or per batch, so a crash leaves a job exactly where it was and the next tick picks it up. A host throttle (429/999) returns a description to the queue with no per-job penalty; a job-specific refusal (401/403/503/504) or unreadable markup keeps its 30 minutes to 12 h backoff; a job expires after seven days of host refusals, when the posting is over 30 days old, or when it has waited 96 h for a description in tiers 3 and 4 as a non-AI posting (reversible: `stage_reason = aged_out`, `age_out.enabled: false` switches the rule off).
- **Resilience by design**: batch-level retry on 5xx/timeout, including OpenRouter's habit of surfacing an upstream 5xx as HTTP 200 with a JSON error body, distinguished in code (`_is_retryable_upstream_value_error` in `src/scorer.py`) from an unrelated bug before retrying. A quota day stops the LLM stages and leaves jobs waiting, not lost.
- **Shared LinkedIn pace**: the optional closed window (off as shipped), the shared daily counter, the fixed jittered spacing, the refusal waits, the two-of-three description turns and the packaging turn all live in one place (`src/linkedin/pace.py`), enforced by a Postgres advisory lock, and every request is appended to `pace_events`.
- **Anti-hallucination in code, not just the prompt**: the CV is checked at runtime against the canonical source, not left to an instruction the model can ignore. The cover letter is checked for banned phrases, length, a concrete bridge, and a first-person voice. Hook claims are listed in `CLAIMS_REVIEW.txt` for a person to confirm.

## By the numbers

| Metric | Value | Verified via |
|---|---|---|
| Geographic tiers | **4** | `config/config_tier{1..4}.json` |
| LinkedIn search slices | **127** (role x area) | built from the tier configs and `config/collector.json` |
| Max pages per slice | **90** | `config/collector.json` |
| Per-area page cap | **20 for the ten largest tier-3 countries** | `config/collector.json` |
| Daily LinkedIn request cap | **none** (the pace is the limit) | `config/schedule.json` |
| Score cutoff | **8** | `scoring.threshold` in each tier config |
| Manual steps in the daily run | **0** | systemd services |

Tier 1 is Italy full-remote, tier 2 Switzerland and San Marino in any work mode, tier 3 the EU/EEA full-remote workable from Italy, tier 4 the UK full-remote. LinkedIn's guest endpoint silently ignores its work-mode filter (`f_WT`), so the collector cannot ask for remote-only postings; the remote check is what enforces the work mode for tiers 1, 3 and 4. The tier configs therefore carry no `search.work_mode` key.

## Run it

```bash
pip install -r requirements.txt
playwright install chromium
cp .env.template .env
```

Set `JOBSTORE_URL` (the server PostgreSQL store) plus `LLM_API_KEY`, `GROQ_API_KEY`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `DATABASE_URL` (Neon archive), and `OUTPUT_PATH`. Tailoring also needs `CV_MASTER_PATH`, `CV_CSS_PATH`, `CL_MASTER_PATH`, and `JD_OUTPUT_ROOT`; the CV and cover-letter source files are not in this repo. `deploy/RUNBOOK.md` covers provisioning and the systemd units.

```bash
python -m src.linkedin.collector --sync-slices   # create/refresh the slices, then exit
python -m src.linkedin.collector                 # run the collector
python -m src.processor.runner --once            # one processor tick (drop --once to run forever)
python -m src.delivery.service --once            # one delivery tick
python -m src.sources.service freehire --sync-slices
python -m src.services.status                    # operator view of the store, pace and services
python -m src.services.watchdog                  # one health check
python tailor.py path/to/note.md
python -m pytest tests/
```

## Roadmap

- **Hybrid RAG** (BM25 + dense + reranker) over the corpus and my profile.
- **Eval framework**: LLM-as-judge, retrieval metrics (recall@k, MRR), and an honest write-up of where it wins and loses.
- **Production layer**: a FastAPI service, Docker, and an observability dashboard. Test CI and per-run telemetry are already in place.

---

*A personal tool: local paths and CV source content are wired to my own machine, so the repo is here primarily to read. Built and maintained by [Enrico Roncuzzi](https://www.linkedin.com/in/enricoroncuzzi/), AI/ML Engineer, MSc Computer Science @ Politecnico di Milano.*
