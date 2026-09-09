# Project agent memory

jd-scraper is an AI job-hunt pipeline with three subsystems. See `README.md` for
the full product description and architecture diagram; this file only covers
what the README doesn't (or what has drifted from it).

## Subsystems

1. **Scraper -> verifier -> scoring -> corpus** (`main.py`, `orchestrator.py`,
   `src/`): a 4-tier LinkedIn scraper (paginated per query up to a page cap,
   see `_MAX_PAGES_PER_QUERY` in `src/scraper.py` - a *card* budget, not a
   page-count one, because `_fetch_for_query` advances `start` by the number
   of cards each response actually returned; the guest endpoint served 25
   cards per request until 2026-09 and 10 now, and a constant 25 stride
   silently skipped ~60% of every window, so never re-introduce a page-size
   assumption here), language filter, dedup,
   remote verification, LLM scoring (OpenRouter, free-tier models with a
   native model fallback array - see `_OPENROUTER_MODEL`/
   `_OPENROUTER_FALLBACK_MODELS` in `src/scorer.py`), Postgres (Neon) storage,
   then a per-tier `digest.md` + `rejected.md` audit file in Obsidian, plus a
   Telegram summary. The 4 tiers (`config/config_tier{1..4}.json`) are not a
   uniform geographic sweep: tier 1 is Italy full-remote, tier 2 is
   Switzerland/San Marino any work mode, tier 3 is EU/EEA full-remote (via a
   scope filter), tier 4 is United Kingdom full-remote - see each tier config's
   `search`/`remote_check` block for the exact filters. Remote verification
   (`src/remote_verifier.py::verify_offers`) runs after scraping/dedup but
   before scoring, on Groq (a separate key/quota from the OpenRouter scorer)
   rather than OpenRouter, and rules each offer confirmed/rejected/unconfirmed;
   it is a filter, not a gate - every failure mode (missing key, empty
   description, a batch that won't complete) resolves to unconfirmed rather
   than silently dropping a real job. `orchestrator.py` runs the 4 tier
   configs sequentially via `main.py`. Each tier's CLI entrypoint (`main.py`'s
   `run_tier_with_retry`) retries an uncaught transient failure (scraper/scorer
   exception) with quota-aware exponential backoff via `src/retry.py`'s
   `run_with_backoff` - it never retries OpenRouter daily-quota exhaustion
   (reuses `src/scorer.py`'s `_is_quota_exceeded`, see below), and a final
   give-up sends a Telegram failure notification
   (`main.py`'s `_notify_failure`) so it isn't just a cron log line. Scoring
   migrated from Cerebras to OpenRouter in
   2026-08 after Cerebras killed its permanent free tier; OpenRouter's $0 tier
   caps at 50 requests/day account-wide (not per-model, not per-key - the
   whole account), and its 429 error body has no Cerebras-style string code to
   tell a same-day cap exhaustion apart from a transient per-minute/upstream
   throttle - see `_is_quota_exceeded` in `src/scorer.py` for the actual
   distinguishing signal (how far away `X-RateLimit-Reset` is). `_invoke_batch`'s
   retry loop also catches 5xx/timeout (`openai.InternalServerError`/
   `APIConnectionError`) at the batch level, not just 429s: an uncaught 5xx used
   to promote to a full tier restart via `run_tier_with_retry` instead of a
   batch-level retry (confirmed root cause of the 2026-09-01 outage). OpenRouter
   sometimes surfaces an upstream 5xx as HTTP 200 with a JSON error body rather
   than an actual 5xx status, which `langchain_openai` turns into a plain
   `ValueError` instead of `openai.InternalServerError` - see
   `_is_retryable_upstream_value_error` in `src/scorer.py` for how that case is
   told apart from an unrelated `ValueError` (a real bug) before retrying.
   `with_structured_output(method="function_calling")` also returns `None`
   (not an exception) when the model skips the forced tool call - `_invoke_batch`
   treats that (`_EmptyStructuredOutput`) as a retryable batch failure with the
   same backoff shape as the other branches; before 2026-09, this escaped as an
   uncaught `AttributeError` and triggered a full tier restart via
   `run_tier_with_retry` instead of a batch retry (confirmed root cause of
   repeated tier restarts around 2026-09-06 to 2026-09-08). Remote verification
   (`src/remote_verifier.py`) sends Groq a keyword-anchored excerpt of each
   description (`_extract_policy_excerpt`), not a flat character prefix - a
   flat cutoff both overspent Groq's 200,000-token/day account-wide cap at
   post-reshape volume and silently missed the remote/hybrid/on-site sentence
   on postings where it appears late (measured on real postings, worse than
   the cutoff itself in some cases). `BATCH_SIZE` there (8) is intentionally
   larger than the scorer's (5) to amortize the fixed per-batch prompt
   overhead now that per-offer cost is much smaller; `_MAX_DESC_CHARS` is both
   the no-keyword-found fallback prefix length and the per-offer excerpt budget
   cap (joiners included), so changing it moves the whole stage's daily token
   spend. When the keyword windows don't fit the budget, the context radius
   shrinks uniformly instead of the excerpt being filled in document order -
   otherwise early remote-flavoured boilerplate crowds out a decisive late
   on-site sentence.
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
   link; (c) writes a package manifest and fires a Telegram notification for
   the captain to review and submit manually
   (`src/autoapply/package.py::notify_package`, via
   `src/telegram.py::send_message` - the same channel/token the daily digest
   uses, not `src/tailor/notify.py`'s macOS-only osascript/open, which only
   fires from the one-click `tailor.py` CLI run by a human, not from
   unattended cron).
   `src/storage.py`'s `applications` table (keyed by an md5 link hash, same
   scheme as `src/dedup.py`) is the application-time dedup gate - distinct
   from `src/dedup.py`'s scrape-time dedup - and gates the per-day cap
   (`config.autoapply.daily_cap`) via
   `count_applications_packaged_today`/`is_application_packaged`.
   `config.autoapply.dry_run` (default `true`) runs the full pipeline
   (classify, tailor, package) but skips only the Telegram notification, for
   safe testing - it still writes the `applications` tracking row (with
   `dry_run=true`), because `is_application_packaged`/
   `count_applications_packaged_today` don't distinguish dry-run from live rows:
   without that write, the same still-open offer got re-tailored (and
   re-billed against the Groq quota) every day dry-run stayed on. One
   consequence: once an offer is dry-run-packaged it stays deduped even after
   `dry_run` flips to `false` - it will never retroactively fire a live
   notification for that offer, only newly-qualifying ones do.
   `main.py`'s call into `run_autoapply` is wrapped in `try/except` (mirroring
   `run_tier_with_retry`'s failure notification): a tailoring/notify failure
   sends a Telegram "auto-apply FAILED" message and lets the tier's regular
   digest still go out, instead of crashing the whole run. See
   `data/jds-autoapply-explore/report.md` in the firstmate home (not this
   repo - it's outside the jd-scraper worktree) for the full design
   rationale and the captain's approval; treat any change to auto-submit
   scope or to `src/tailor/validate.py`'s gate itself as requiring a fresh
   captain decision, not a worker judgment call.

## Config and secrets

- Scraper tier configs live in `config/config_tier1.json` .. `config_tier4.json`
  (one per tier); `config/config.example.json` is the template for the
  gitignored `config/config.json`.
- Each tier's geographic scope filter is config-driven: `search.allowed_countries`
  lists the countries its results must resolve to (canonical or display names,
  mapped by `src/tier_scope.py::resolve_allowed_countries`, which raises on an
  unknown name); a tier with no such list does no narrowing. This replaced the
  old hardcoded `config.tier == 3` binding in `main.py`. The bare search string
  "San Marino" geo-resolves to San Marino, California on LinkedIn's guest API, so
  tier 2's `search.countries` must use "San Marino, San Marino" to reach the
  republic; two-letter US state tails ("El Segundo, CA") resolve to
  "united states" in `resolve_country` and are discarded by scopes that do not
  allow them, except for the tails that are also a country ISO2 or Swiss canton
  code and so stay unresolvable on purpose (see `_AMBIGUOUS_TWO_LETTER_TAILS`).
- Runtime secrets are read from a gitignored `.env`; `.env.template` lists the
  expected keys. `LLM_API_KEY` is read generically (the `llm_api_key`
  assignment in `src/config.py`'s `load_config`, not provider-specific by
  name) and currently holds an OpenRouter key consumed by
  `src/scorer.py`'s scoring calls. `GROQ_API_KEY` is consumed by three
  paths: the tailoring engine (`src/tailor/generate.py`), auto-apply
  (`src/autoapply/pipeline.py`, via `main.py`), and remote verification
  (`src/remote_verifier.py`, via `main.py`'s call into `verify_offers`). Its absence does not fail the
  run loudly - `verify_offers` degrades to marking every offer unconfirmed,
  which then silently blocks auto-apply for tiers with `remote_check.enabled`
  (the candidate filter in `src/autoapply/pipeline.py` excludes unconfirmed
  offers). Check `src/config.py`, `src/scorer.py`, `main.py`, and `tailor.py`
  for the actual env vars consumed rather than trusting the template.
- The CV source content `tailor.py` tailors from (`CV_master.md`, `CV_css.md`)
  is not part of this repo - it's personal content that lives outside git.
  `tailor.py`'s `_DEFAULT_MASTER`/`_DEFAULT_CSS`/`_DEFAULT_ROOT` read
  `CV_MASTER_PATH`/`CV_CSS_PATH`/`JD_OUTPUT_ROOT` env vars first, falling back
  to a hardcoded path under one specific machine's home directory only if
  unset - that fallback only resolves on the machine it was written for, so
  every other host (a VPS included) must set these three env vars and have the
  actual CV source files placed at those paths, or `run_autoapply`
  (`src/autoapply/pipeline.py`) raises `FileNotFoundError` up front rather than
  silently no-op'ing per-offer. Never commit the source files themselves.

## Deploying to the VPS

- Deploy is manual: SSH in, `git pull` on `main`, `pip install -r
  requirements.txt`, restart nothing (the pipeline only runs via the daily
  cron job, not a long-lived process). There is no CI/CD pipeline verifying
  the VPS tracks `origin/main` - deploy drift (VPS behind `main` for days) has
  happened more than once; re-check `git rev-parse HEAD` vs `origin/main`
  whenever "it isn't working" comes up before assuming a code bug.
- Playwright's Chromium (used by `src/tailor/render_pdf.py` for CV/cover-letter
  PDF rendering) needs two separate provisioning steps beyond `pip install`,
  neither of which is automated anywhere in this repo: `playwright install
  chromium` (downloads the browser binary) and, on a bare Debian/Ubuntu box,
  `playwright install-deps chromium` as root (installs the OS shared libraries
  Chromium needs to actually launch - missing them fails headless launch with
  `error while loading shared libraries: libnspr4.so...`, not a Python
  exception). Re-run both after provisioning a new box.
- The CV's reference CSS (`src/tailor/render_pdf.py`) names Tahoma/Georgia,
  which aren't installed on a bare Debian box (`ttf-mscorefonts-installer` is
  not in Debian's default repos, only `contrib`) - PDFs still render, just
  with fontconfig's fallback substitution rather than the intended fonts. A
  cosmetic fidelity gap, not a functional one; unresolved as of 2026-08.

## Tests

Run with `.venv/bin/python -m pytest tests/`. `tests/conftest.py` already puts
the repo root on `sys.path` — don't add a second conftest for that purpose.

## CI

`.github/workflows/tests.yml` runs the pytest suite on every pull request
(and on push to `main`) - one job, no matrix, no secrets. The Python
version is pinned to match the production VPS's `.venv` (see "Deploying
to the VPS" above), not a developer's local `.venv` - update the pin when
the server's Python moves, not when a laptop's does. The suite must pass
with no API keys set (Groq/OpenRouter calls are mocked in tests); if a
test ever needs a live key, that's a test-design problem, not something
to fix by adding a secret to the workflow.

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
