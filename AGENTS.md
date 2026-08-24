# Project agent memory

jd-scraper is an AI job-hunt pipeline with two subsystems. See `README.md` for
the full product description and architecture diagram; this file only covers
what the README doesn't (or what has drifted from it).

## Subsystems

1. **Scraper -> scoring -> corpus** (`main.py`, `orchestrator.py`, `src/`): a
   4-tier LinkedIn scraper, language filter, dedup, LLM scoring (OpenRouter,
   free-tier models with a native model fallback array - see
   `_OPENROUTER_MODEL`/`_OPENROUTER_FALLBACK_MODELS` in `src/scorer.py`),
   Postgres (Neon) storage, then an Obsidian digest + Telegram summary.
   `orchestrator.py` runs the 4 tier configs in `config/config_tier{1..4}.json`
   sequentially via `main.py`. Scoring migrated from Cerebras to OpenRouter in
   2026-08 after Cerebras killed its permanent free tier; OpenRouter's $0 tier
   caps at 50 requests/day account-wide (not per-model, not per-key - the
   whole account), and its 429 error body has no Cerebras-style string code to
   tell a same-day cap exhaustion apart from a transient per-minute/upstream
   throttle - see `_is_quota_exceeded` in `src/scorer.py` for the actual
   distinguishing signal (how far away `X-RateLimit-Reset` is).
2. **CV tailoring engine** (`tailor.py`, `src/tailor/`): tailors a
   CV/cover-letter/recruiter message per job posting. The CV body is never
   rewritten - `src/tailor/cv_master.py`'s `assemble()` selects and reorders
   verbatim bullets/skills from a hand-written canonical CV; only the cover
   letter's hook/bridge and the recruiter message are freely generated text
   (see the prompt in `src/tailor/generate.py`). `src/tailor/validate.py`
   is the validation gate: it byte-matches the assembled CV against
   `REQUIRED_METRICS` and checks cover-letter claims, halting on a mismatch.
   Generation uses **Groq** (`openai/gpt-oss-120b`, key read as
   `GROQ_API_KEY` in `tailor.py`).
3. **Auto-apply v1 (draft-and-notify)** (`src/autoapply/`): a
   **draft-and-notify system, not an auto-submit system - there is no code
   path anywhere that submits an application.** Wired into `main.py` behind
   `config.autoapply.enabled` (default `false`; see
   `config/config.example.json`). When enabled, for each offer scoring
   `>= config.scoring.threshold` it: (a) resolves the offer's `link` and
   classifies the application channel via a read-only HTTP GET + redirect
   follow (`src/autoapply/classify.py::classify_channel` - never logs in,
   never drives a browser, never touches LinkedIn's or an ATS's UI); (b)
   auto-invokes the existing one-click `tailor.py` flow (`tailor_cli.run()`,
   unmodified) instead of waiting for a human to click the digest's `tailor:`
   link; (c) writes a package manifest and fires a notification for the
   captain to review and submit manually
   (`src/autoapply/package.py`, extending `src/tailor/notify.py`'s pattern).
   `src/storage.py`'s `applications` table (keyed by an md5 link hash, same
   scheme as `src/dedup.py`) is the application-time dedup gate - distinct
   from `src/dedup.py`'s scrape-time dedup - and gates the per-day cap
   (`config.autoapply.daily_cap`) via
   `count_applications_packaged_today`/`is_application_packaged`.
   `config.autoapply.dry_run` (default `true`) runs the full pipeline
   (classify, tailor, package) but skips the notification and the tracking
   write, for safe testing. See
   `data/jds-autoapply-explore/report.md` in the firstmate home (not this
   repo - it's outside the jd-scraper worktree) for the full design
   rationale and the captain's approval; treat any change to auto-submit
   scope or to `src/tailor/validate.py`'s gate itself as requiring a fresh
   captain decision, not a worker judgment call.

## Config and secrets

- Scraper tier configs live in `config/config_tier1.json` .. `config_tier4.json`
  (one per tier); `config/config.example.json` is the template for the
  gitignored `config/config.json`.
- Runtime secrets are read from a gitignored `.env`; `.env.template` lists the
  expected keys. `LLM_API_KEY` is read generically (`src/config.py:58`, not
  provider-specific by name) and currently holds an OpenRouter key consumed by
  `src/scorer.py`'s scoring calls; `GROQ_API_KEY` is unrelated, used only by
  the tailoring engine (`src/tailor/generate.py`). Check `src/config.py`,
  `src/scorer.py`, and `tailor.py` for the actual env vars consumed rather
  than trusting the template.

## Tests

Run with `.venv/bin/python -m pytest tests/`. `tests/conftest.py` already puts
the repo root on `sys.path` — don't add a second conftest for that purpose.

## No CI

There is no CI configured (no `.github/workflows/`). Verification is manual:
run the pytest suite above before considering a change done.

## Historical planning docs

`.superpowers/sdd/` and `obs-jds/` (both gitignored, not tracked in this repo)
hold phase-by-phase design docs from this project's earlier, pre-Firstmate
workflow, including an unimplemented "Phase 4 autonomous application agent"
plan. Treat them as historical context only, not binding scope.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
