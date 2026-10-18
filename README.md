# jd-scraper - grounded LLM pipeline for AI/ML job hunting

> A production job-hunt pipeline that collects AI/ML postings across four regions, verifies remote eligibility and scores fit with structured LLM output, keeps everything in a server-side Postgres job store (archived to Neon), and tailors a CV, cover letter, and recruiter message per posting, with every CV bullet taken from the real CV.

Built solo, test-driven, running unattended in production as systemd services.

## The interesting part: grounded generation, enforced in code

The hard problem here isn't collecting jobs, it's making an LLM produce text a hiring manager can trust. Three design choices do that:

- **Structured output everywhere an LLM touches the pipeline.** Scoring (`src/scorer.py`), remote-eligibility verification (`src/remote_verifier.py`), and CV tailoring (`src/tailor/generate.py`) each define a Pydantic schema and parse the model's response into it. No regex on free text, no brittle string parsing.
- **Anti-hallucination enforced at runtime, not by prompt wording.** `src/tailor/cv_master.py`'s `assemble()` never asks the LLM to write CV content: every bullet is included in canonical order, and only the skill groups are reordered, verbatim, from a hand-written canonical CV. The LLM only writes the cover-letter hook/bridge and the recruiter message. `src/tailor/validate.py` then byte-matches the assembled CV against six required metrics (e.g. `"94.1%"`, `"10.5M"`) and halts the run if one is missing. The cover letter is checked for banned phrases, length, and a concrete bridge. Hook claims are written to `CLAIMS_REVIEW.txt` for a person to confirm, rather than trusting the model to have been careful.
- **A server-side Postgres job store** (`src/jobstore/schema.py`) that is the only state the collector, processor and delivery services share, with every stage committing per job or per batch so a restart resumes where it stopped. Finished jobs are copied in batches to a Neon archive (`src/delivery/archive.py`) that holds the scored, full-text corpus, the kind of ground-truth data set an eval or retrieval layer would need, growing daily from real traffic rather than a scraped-once snapshot.

## How it runs today: a job store and long-running services

The production path is no longer a once-a-day cron run. It is long-running services around a PostgreSQL job store on the server. Services never call each other; the store is their only shared state. The install, switch-over and rollback procedure, including the systemd units in `deploy/systemd/`, lives in [`deploy/RUNBOOK.md`](deploy/RUNBOOK.md).

- **LinkedIn collector** (`jds-collect-linkedin`, `src/linkedin/collector.py`) - walks "slices", one per role and area, built from the four tier configs by `src/linkedin/catalog.py` into the `slices` table, and records every card as a sighting. It never fetches a description and never judges a job.
- **Daily source collectors** (`jds-collect-freehire`, `jds-collect-himalayas`, `src/sources/`) - one oneshot pull a day each (03:00 and 04:00 server time) from freehire.me and Himalayas, whose public JSON APIs return full descriptions inline, plus `jds-resume-freehire.timer` and `jds-resume-himalayas.timer` every 30 minutes to finish an interrupted walk. Configured in `config/sources.json`.
- **Processor** (`jds-process`, `src/processor/runner.py`) - the stage machine: expire stale jobs, cheap no-network checks (tier assignment, cross-source merge, title pre-filter), the remote check (a deterministic rule pre-pass, then the LLM for what the rules will not settle; off for tier 2, which accepts any work mode), LLM scoring, then LinkedIn description fetches through the shared pace, with the language check applied as soon as a job has a description. Batch sizes, backoff and tick limits are in `config/processor.json`; the score cutoff, keywords and candidate profile come from the tier configs.
- **Delivery** (`jds-deliver`, `src/delivery/`) - immediate Telegram alerts for jobs at or above the cutoff (not for a job whose work mode is not stated), the daily delivery at 08:00 Europe/Rome (one digest per tier plus a day summary with collection, budget and service health), auto-tailoring of above-cutoff confirmed jobs (a package per job; the Telegram notice stays off while `autoapply.dry_run` is true, as shipped), and the periodic Neon archive copy. Each day also lands in `OUTPUT_PATH/<date>/tier<N>/` as one note per job, a ranked `digest.md` whose remote-checked sections are "Confirmed full-remote", "Remote, eligibility to check" and "Work mode not stated", and a `rejected.md` audit trail of what the remote check screened out and why. Timing and expectations are in `config/deliver.json`.
- **Watchdog** (`jds-watchdog.timer`, `src/services/watchdog.py`) - every ten minutes, messages Telegram when a service has been silent for over an hour, a daily source collector has missed its run by more than three hours (or ended in error), no LinkedIn request has succeeded for 24 h of open time (at most one alert a day), or the job store is unreachable.
- **Nightly backup** (`jds-backup.timer`, `deploy/backup-jobstore.sh`) - a `pg_dump` of the job store, keeping the seven newest dumps.

`LinkedIn slices -> sightings -> cheap checks -> description -> language -> remote check -> score -> delivery (digest + alerts) -> [tailor] -> OpenRouter generation + validation gate -> PDF -> Neon archive`

The `jobs` table is the stage machine: `seen -> described -> filtered_out | scored -> delivered | expired`. `sightings` records every source that saw a posting, `job_applications` the packaged ones, and `service_heartbeats`/`pace_events`/`llm_calls` the operational record. The pre-redesign entrypoints (`main.py`, `orchestrator.py`, `src/scraper.py`) still ship during the switch-over window; `deploy/RUNBOOK.md` covers their retirement.

## The LinkedIn schedule (schedule C)

Every LinkedIn request the collector, the processor and the packaging stage make passes one choke point, `src/linkedin/pace.py`, so they cannot exceed a shared budget together or have two requests in flight. `config/schedule.json` sets the policy:

- quiet hours 00:00-06:00 Europe/Rome;
- a shared daily budget of 1,400 requests on weekdays and 800 at weekends, on the Rome calendar day;
- a fixed pace of 75 s weekday / 110 s weekend, jittered +/-20 %, never below the cap-derived floor (46 s / 81 s): nothing slows it down (the 2026-10-18 refusal study found the refusal rate did not depend on the gap, while the old nudge and day verdict cut throughput to about a quarter);
- a refusal is a short capped wait (60 s doubling to 30 min, or `Retry-After`), never a shutdown and never a change of pace;
- descriptions take two of every three turns when a search is also waiting (`pace.description_turn_share`), with a 24 h guard that lets an overdue description spend more than one processor tick;
- every area walked within its cadence, with a 6 h tolerance on the 48 h bound.

Slices carry their own cadence (`config/collector.json`, `due_hours` 12/24/48/24 plus per-area overrides, bounded by `min_interval_hours`/`max_interval_hours`). The ten large tier-3 countries are walked at most 20 pages per walk. freehire and Himalayas use the same patient approach: jittered 1-2 s / 2-5 s spacing and a bounded 429 wait, with the 30-minute resume timers for an unfinished walk. The live gate, budget and pace are in `python -m src.services.status`, and `deploy/RUNBOOK.md` carries the config tables and the deploy order.

## LLM stages and caps

Remote verification runs on OpenRouter (`nvidia/nemotron-3-super-120b-a12b:free`, with a three-model native fallback chain) and falls back to Groq `openai/gpt-oss-20b`. Verification is capped at 4/10 of the account-wide OpenRouter allowance (400 of 1,000 requests), and only that stage is capped. It is a filter, not a gate: nothing it decides drops a job silently, and a stage failure is deferred and retried instead of being stored as a verdict.

The check is two steps (design decision 2026-10-18, after the full-remote scout):

1. **A free deterministic pre-pass** (`src/remote_rules.py`) reads the description, title, location and the source's own work mode. Confident hybrid/on-site phrases in English, Italian, Spanish, French and German reject; confident fully-remote phrases ("fully remote", "remote-first", "100% teletrabajo", "vollständig remote") confirm; a description with no work-mode vocabulary at all is `not_stated` without a model call. Hybrid/on-site from freehire/Himalayas is a shortcut (99.8% agreement on 420 decided jobs); the source's *confirm* shortcuts (87.3% and 66.7%) stay off. Borderline wording ("smart working", "hybrid or remote", a negated phrase) always goes to the model.
2. **One structured model answer** with `work_mode` (remote/hybrid/onsite/flexible/not_stated), `eligibility` (open/restricted/not_stated) and a quote copied from the description. The model reads the whole description instead of the old 1,000-character keyword excerpt, bounded at 6,000 characters and, for a full 8-offer batch, about 3,000 characters each so the request stays inside Groq's free per-minute token limit. A quote that is not found in the text is treated as `not_stated`, never as a rejection. `remote-first` counts as fully remote.

The verdict vocabulary is in `src/remote_verdicts.py`: `confirmed` (fully remote, workable from Italy), `advised` (fully remote but limited to another country, or with no stated country restriction in tiers 3/4 - **never rejected**, delivered and alerted with a short advice line), `not_stated` (the text never says; **kept in the digest in its own section and never alerted on Telegram**), `rejected` (hybrid, on-site, or remote only as an occasional perk), and `not_checked` (tier 2). The legacy `unconfirmed` value is still accepted and reads as `not_stated`. Only `confirmed` and `not_checked` are auto-tailored.

Scoring uses the same OpenRouter primary and fallback chain. Daily provider limits live in `config/llm_limits.json` (Groq 200,000 tokens; OpenRouter 1,000 free-model requests per account), and the processor stops the LLM stages at `llm_stop_at_requests` (950 today) to leave headroom for tailoring. There is no paid model and no local model: both were weighed in the scout and declined (D4/D5).

## CV tailoring, working today

`tailor.py <job>` calls OpenRouter (`nvidia/nemotron-3-super-120b-a12b:free`) to reorder skill groups from a canonical CV for the specific job description. Every bullet is included, in canonical order. The model writes only the cover letter's hook/bridge and the recruiter message as free text. Everything else in the CV is copied byte-for-byte. The validation gate in `src/tailor/validate.py` aborts the run if a required metric is missing, or if the cover letter fails its checks for banned phrases, length, and a concrete bridge. Hook claims are written to `CLAIMS_REVIEW.txt` for a person to confirm. Headless Chromium then renders CV, cover letter, and recruiter message to PDF, reproducing the original template offline, and the CV must stay a single page. The package is one directory per offer.

The delivery service runs the same engine automatically for above-cutoff jobs: it writes the package and, once `autoapply.dry_run` is switched off in the tier config, sends a Telegram notice. It is a draft-and-notify system with no code path that submits an application. Each digest entry also has ⭐ golden and 👎 not for me links. After a confirmation dialog they record the offer in the `offer_labels` table (one label per posting, re-clicking is a no-op), building a labelled set for evals. `python label.py --list golden` prints it and `python label.py --remove golden <link-or-note>` undoes a label. The handler is installed by `scripts/install-tailor-app.command`.

## Tech stack

`Python` - `OpenRouter` & `Groq` LLM APIs - `Pydantic` - `PostgreSQL` (server job store + Neon archive) - `systemd` - `Playwright` (headless Chromium) - `LangChain` (thin scoring adapter) - `BeautifulSoup` (source parsing) - `pytest` (TDD)

## Engineering notes

- **Test-driven throughout**: `.venv/bin/python -m pytest tests/`, every module built red to green. Count them with `pytest tests/ --collect-only -q`; the number moves.
- **Structured LLM I/O everywhere**: Pydantic schemas for scoring, remote verification, and tailoring, no string-parsed output.
- **No silent loss**: every stage transition is one `UPDATE` in the caller's transaction, and each stage commits per job or per batch, so a crash leaves a job exactly where it was and the next tick picks it up. A host throttle (429/999) returns a description to the queue with no per-job penalty; a job-specific refusal (401/403/503/504) or unreadable markup keeps its 30 minutes to 12 h backoff; a job expires after seven days of host refusals or when the posting is over 30 days old.
- **Resilience by design**: batch-level retry on 5xx/timeout, including OpenRouter's habit of surfacing an upstream 5xx as HTTP 200 with a JSON error body, distinguished in code (`_is_retryable_upstream_value_error` in `src/scorer.py`) from an unrelated bug before retrying. A quota day stops the LLM stages and leaves jobs waiting, not lost.
- **Shared LinkedIn pace**: the quiet window, the shared daily budget, the fixed jittered spacing, the refusal waits and the two-of-three description turns all live in one place (`src/linkedin/pace.py`), enforced by a Postgres advisory lock, and every request is appended to `pace_events`.
- **Anti-hallucination in code, not just the prompt**: the CV is checked at runtime against the canonical source, not left to an instruction the model can ignore. The cover letter is checked for banned phrases, length, and a concrete bridge. Hook claims are listed in `CLAIMS_REVIEW.txt` for a person to confirm.

## By the numbers

| Metric | Value | Verified via |
|---|---|---|
| Geographic tiers | **4** | `config/config_tier{1..4}.json` |
| LinkedIn search slices | **127** (role x area) | built from the tier configs and `config/collector.json` |
| Max pages per slice | **90** | `config/collector.json` |
| Per-area page cap | **20 for the ten largest tier-3 countries** | `config/collector.json` |
| Shared daily LinkedIn budget | **1,400 weekday / 800 weekend** requests | `config/schedule.json` |
| Score cutoff | **8** | `scoring.threshold` in each tier config |
| Manual steps in the daily run | **0** | systemd services |

Tier 1 is Italy full-remote, tier 2 Switzerland and San Marino in any work mode, tier 3 the EU/EEA full-remote workable from Italy, tier 4 the UK full-remote. LinkedIn's guest endpoint silently ignores its work-mode filter (`f_WT`), so the collector cannot ask for remote-only postings; the remote check is what enforces the work mode for tiers 1, 3 and 4. The tier configs therefore carry no `search.work_mode` key.

## Run it

```bash
pip install -r requirements.txt
playwright install chromium
cp .env.template .env
```

Set `JOBSTORE_URL` (the server PostgreSQL store) plus `LLM_API_KEY`, `GROQ_API_KEY`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `DATABASE_URL` (Neon archive), and `OUTPUT_PATH`. Tailoring also needs `CV_MASTER_PATH`, `CV_CSS_PATH`, and `JD_OUTPUT_ROOT`; the CV source files are not in this repo. `deploy/RUNBOOK.md` covers provisioning and the systemd units.

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
