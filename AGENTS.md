# Project agent memory

jd-scraper is an AI job-hunt pipeline. `README.md` describes the product and
architecture, `deploy/RUNBOOK.md` the install, deploy, rollback and LinkedIn pace
configuration. This file only holds the standing rules and a short map.

## Map

- Production is a set of always-on systemd services (`deploy/systemd/`) around the
  server-side PostgreSQL job store `jds` (`JOBSTORE_URL`, `src/jobstore/`). Services
  never call each other; the store is their only shared state. Neon (`DATABASE_URL`)
  is only the archive that `jds-deliver` copies finished jobs into
  (`src/delivery/archive.py`).
- `jds-collect-linkedin` (`src/linkedin/`) walks search slices and records sightings.
  `jds-collect-freehire` / `jds-collect-himalayas` plus their `jds-resume-*` timers
  (`src/sources/`) pull the other sources. `jds-process` (`src/processor/`) runs the
  stage machine: cheap checks, descriptions, language, the remote check
  (`src/remote_rules.py` rule pre-pass, then `src/remote_verifier.py`), scoring
  (`src/scorer.py`). `jds-deliver`
  (`src/delivery/`) sends alerts and the daily digest, auto-tailors above-cutoff jobs
  and copies to Neon. `jds-watchdog` and `jds-backup` watch and back up.
  `python -m src.services.status` shows all of it.
- The 4 tiers (`config/config_tier{1..4}.json`) are not a uniform sweep: tier 1 is
  Italy full-remote, tier 2 Switzerland/San Marino any work mode, tier 3 EU/EEA
  full-remote (via `search.allowed_countries`), tier 4 United Kingdom full-remote.
  Tier 2's `search.countries` must say "San Marino, San Marino": the bare name
  geo-resolves to San Marino, California. The tier configs carry no
  `search.work_mode`: LinkedIn's guest endpoint silently ignores `f_WT`, so the
  remote check is the only work-mode filter for tiers 1, 3 and 4.
- CV tailoring is `tailor.py` and `src/tailor/`.
- `main.py`, `orchestrator.py`, `src/scraper.py`, `src/dedup.py`, `src/retry_queue.py` and
  `src/autoapply/pipeline.py` are the old daily-cron pipeline. They still ship until
  `deploy/RUNBOOK.md` section 8 retires them; the services do not use them.

## Standing rules

- **Nothing submits an application.** Auto-tailoring is draft-and-notify: it classifies
  the link with a read-only GET (`src/autoapply/classify.py`: never logs in, never
  drives a browser, never touches LinkedIn's or an ATS's UI), tailors, writes a
  package and a `job_applications` row (`src/delivery/autopackage.py`), and notifies
  the captain by Telegram to submit by hand. `autoapply.dry_run` (tier config, default
  `true`) skips only that notification; the row is still written, so a dry-run-packaged
  job is never packaged again, even after `dry_run` flips to `false`.
- The CV body is never rewritten: `src/tailor/cv_master.py`'s `assemble()` selects and
  reorders verbatim text, and `src/tailor/validate.py` is the gate. Any change to
  auto-submit scope or to that gate needs a fresh captain decision, not a worker
  judgment call.
- Do not change LinkedIn's request headers (`src/http_session.py`). Every LinkedIn
  request goes through `src/linkedin/pace.py` and `config/schedule.json`; never bypass
  that shared choke point.
- A walk advances `start` by the cards a response actually returned
  (`src/linkedin/walk.py`), never by an assumed page size: the guest endpoint went from
  25 cards per request to 10 in 2026-09, and a fixed stride skipped about 60% of a
  window. Page caps and cadences (`config/collector.json`) are captain decisions.
- The verifier's OpenRouter model pin (`src/remote_verifier.py`) stays independent of
  the scorer's (`src/scorer.py`), so one stage's pin drift does not move the other.
- The remote verdict vocabulary is `src/remote_verdicts.py`, and the captain decided it
  on 2026-10-18: `advised` (remote, eligibility to check) is delivered and alerted with
  an advice line and is **never rejected for eligibility alone**; `not_stated` stays in
  the digest in its own section and **never triggers a Telegram alert**; only
  `confirmed` and tier 2's `not_checked` are auto-tailored; the legacy `unconfirmed`
  value reads as `not_stated`. Do not collapse these back into one verdict.
- The remote check stays on the current free models: a paid hosted model and a local
  model were both weighed in the 2026-10-18 scout and declined (D4/D5).
- Daily provider limits live in `config/llm_limits.json` and are perishable: re-check
  them live against the provider when you change them (`src/llm_limits.py`).

## Config and secrets

- Secrets come from the gitignored `.env` (template: `.env.template`); never commit
  them. Check `src/config.py`, `src/processor/llm_stages.py`, `src/delivery/` and
  `tailor.py` for the env vars actually read. `LLM_API_KEY` is generic and currently
  holds an OpenRouter key (scoring, remote verification, tailoring). `GROQ_API_KEY` is
  only the verification fallback; its absence is not loud, OpenRouter still verifies.
- The CV source files (`CV_master.md`, `CV_css.md`) are personal content outside git;
  never commit them. `tailor.py` reads `CV_MASTER_PATH`, `CV_CSS_PATH` and
  `JD_OUTPUT_ROOT`, with a fallback that only resolves on the author's own machine, so
  every other host must set all three and place the files, or `jds-deliver` raises
  `FileNotFoundError` up front.

## Deploying to the VPS

- Deploy is manual (`deploy/RUNBOOK.md` section 6): `git pull --ff-only`,
  `pip install -r requirements.txt`, restart one unit at a time. Nothing verifies the VPS
  tracks `origin/main`, and deploy drift has happened more than once: when "it isn't
  working" comes up, compare `git rev-parse HEAD` with `origin/main` before assuming a
  code bug.
- Playwright's Chromium (`src/tailor/render_pdf.py`) needs two provisioning steps beyond
  `pip install`, neither automated: `playwright install chromium` and, on bare
  Debian/Ubuntu, `playwright install-deps chromium` as root (missing OS libraries show as
  `error while loading shared libraries: libnspr4.so...`, not a Python exception).
  Re-run both on a new box.
- The CV's reference CSS names Tahoma/Georgia, absent on a bare Debian box; PDFs render
  with fontconfig's fallback. Cosmetic, unresolved as of 2026-08.

## Tests and CI

- Run `.venv/bin/python -m pytest tests/`. `tests/conftest.py` already puts the repo
  root on `sys.path`; do not add a second conftest for that.
- Tests only touch throwaway databases, never production. Job-store tests use
  `JDS_TEST_STORE_URL` or an embedded server (`tests/store_db.py`);
  `tests/test_telemetry_integration.py` needs `JDS_TEST_DATABASE_URL` pointing at a
  throwaway Neon branch (`tests/integration_db.py`).
- `.github/workflows/tests.yml` runs the suite on every pull request and on push to
  `main`, with a throwaway `postgres:17` container (its password is a CI-only literal,
  not a secret). Its Python pin matches the production VPS's `.venv`; update it when the
  server's Python moves, not a laptop's. The suite must pass with no API keys set (LLM
  calls are mocked); a test that needs a live key is a test-design problem, not a reason
  to add a workflow secret.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase, `README.md` or `deploy/RUNBOOK.md` already show; point
to them instead. Prefer rewriting or pruning existing entries over appending new ones,
and keep entries concise.
