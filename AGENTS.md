# Project agent memory

jd-scraper is an AI job-hunt pipeline with two subsystems. See `README.md` for
the full product description and architecture diagram; this file only covers
what the README doesn't (or what has drifted from it).

## Subsystems

1. **Scraper -> scoring -> corpus** (`main.py`, `orchestrator.py`, `src/`): a
   4-tier LinkedIn scraper, language filter, dedup, LLM scoring (Cerebras),
   Postgres (Neon) storage, then an Obsidian digest + Telegram summary.
   `orchestrator.py` runs the 4 tier configs in `config/config_tier{1..4}.json`
   sequentially via `main.py`.
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

## Config and secrets

- Scraper tier configs live in `config/config_tier1.json` .. `config_tier4.json`
  (one per tier); `config/config.example.json` is the template for the
  gitignored `config/config.json`.
- Runtime secrets are read from a gitignored `.env`. `.env.template` lists the
  expected keys but is stale: it still has `GEMINI_API_KEY` even though
  tailoring generation reads `GROQ_API_KEY` (see `tailor.py`). Check
  `src/config.py` and `tailor.py` for the actual env vars consumed rather than
  trusting the template.

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
