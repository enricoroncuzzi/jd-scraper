# jd-scraper - grounded LLM pipeline for AI/ML job hunting

> A production pipeline that scrapes AI/ML job postings across four EU/UK regions daily, verifies remote eligibility and scores fit with structured LLM output, stores everything in a Postgres corpus, and tailors a CV, cover letter, and recruiter message per posting on demand, without inventing a single claim that isn't on the real CV.

Built solo, test-driven (293 tests), running unattended in production.

## The interesting part: grounded generation, enforced in code

The hard problem here isn't scraping, it's making an LLM produce text a hiring manager can trust. Two design choices do that:

- **Structured output everywhere an LLM touches the pipeline.** Scoring (`src/scorer.py`), remote-eligibility verification (`src/remote_verifier.py`), and CV tailoring (`src/tailor/generate.py`) each define a Pydantic schema and parse the model's response into it. No regex on free text, no brittle string parsing.
- **Anti-hallucination enforced at runtime, not by prompt wording.** `src/tailor/cv_master.py`'s `assemble()` never asks the LLM to write CV content: it selects and reorders verbatim bullets and skills from a hand-written canonical CV. The LLM only writes the cover-letter hook/bridge and the recruiter message. `src/tailor/validate.py` then byte-matches the assembled CV against six required metrics (e.g. `"94.1%"`, `"10.5M"`) and checks the cover letter's claims, halting the run on any mismatch, rather than trusting the model to have been careful.
- **A Postgres corpus of scored, full-text job descriptions** (`src/storage.py`), the kind of ground-truth data set an eval or retrieval layer would need, growing daily from real production traffic rather than a scraped-once snapshot.

## What runs daily, zero-touch

1. **Scrape** - four region-scoped LinkedIn sweeps, paginated per query up to 8 pages each (`_MAX_PAGES_PER_QUERY`, `src/scraper.py`): tier 1 Italy full-remote, tier 2 Switzerland/San Marino any work mode, tier 3 EU/EEA full-remote (via a scope filter in `src/tier_scope.py` that excludes whatever the other three tiers already cover), tier 4 United Kingdom full-remote.
2. **Verify** - `src/remote_verifier.py` runs an LLM (via Groq) over each description and rules it confirmed, rejected, or unconfirmed for genuine remote eligibility. It runs before scoring, and every failure mode (no API key, an incomplete batch, an ambiguous description) resolves to unconfirmed instead of silently dropping a real job.
3. **Score** - every remaining posting is rated for fit against my profile by an LLM on OpenRouter (`liquid/lfm-2.5-2.6b:free`, with a 3-model native fallback array), returning structured Pydantic output with a one-line rationale.
4. **Store** - every scored offer persists to a Neon Postgres corpus, full text included.
5. **Digest** - a ranked `digest.md` per tier lands in Obsidian, alongside a `rejected.md` audit trail of what verification screened out and why, plus a Telegram summary.
6. **Tailor** - one click on any offer in the digest runs the CV tailoring engine end to end.

`LinkedIn -> scrape/dedup/filter -> remote verifier (Groq) -> LLM scorer (OpenRouter) -> Postgres corpus -> digest.md + Telegram -> [tailor] -> Groq generation + validation gate -> PDF`

The scraper runs on a VPS via cron. Tailoring runs on demand, one click from the digest.

## CV tailoring, working today

`tailor.py <job>` calls Groq (`openai/gpt-oss-120b`) to select and reorder verbatim bullets and skills from a canonical CV for the specific job description, generating only the cover letter's hook/bridge and the recruiter message as free text. Everything else in the CV is copied byte-for-byte, so the tailored version renders to the same single-page layout as the original. The validation gate in `src/tailor/validate.py` aborts the run if any required metric or claim doesn't match. Headless Chromium then renders CV, cover letter, and recruiter message to PDF, reproducing the original template offline.

## Tech stack

`Python` - `OpenRouter` & `Groq` LLM APIs - `Pydantic` - `PostgreSQL (Neon)` - `Playwright` (headless Chromium) - `LangChain` (thin scoring adapter over OpenRouter) - `pytest` (TDD)

## Engineering notes

- **Test-driven throughout**: 293 tests (`.venv/bin/python -m pytest tests/`), every module built red to green.
- **Structured LLM I/O everywhere**: Pydantic schemas for scoring, remote verification, and CV-section generation, no string-parsed output.
- **Resilience by design**: quota-aware exponential backoff (`src/retry.py`) and batch-level retry on 5xx/timeout, including OpenRouter's habit of surfacing an upstream 5xx as HTTP 200 with a JSON error body, distinguished in code (`_is_retryable_upstream_value_error` in `src/scorer.py`) from an unrelated bug before retrying.
- **Anti-hallucination in code, not just the prompt**: the "every claim traces to the source CV" guarantee is a runtime check, not an instruction the model can ignore.

## By the numbers

| Metric | Value | Verified via |
|---|---|---|
| Automated tests | **293** | `.venv/bin/python -m pytest tests/ --collect-only -q` |
| Geographic tiers | **4** | `config/config_tier{1..4}.json` |
| Max pages per query | **8** | `_MAX_PAGES_PER_QUERY` in `src/scraper.py` |
| Pipeline source lines (`main.py`, `orchestrator.py`, `src/`) | **~2,600** | `wc -l` |
| Manual steps in the daily run | **0** | cron-driven, see `AGENTS.md` |

## Roadmap

- **Hybrid RAG** (BM25 + dense + reranker) over the corpus and my profile.
- **Eval framework**: LLM-as-judge, retrieval metrics (recall@k, MRR), and an honest write-up of where it wins and loses.
- **Production layer**: FastAPI + Docker + CI, and an observability dashboard (cost, latency, hit-rate).

---

*A personal tool: local paths and CV source content are wired to my own machine, so the repo is here primarily to read. Built and maintained by [Enrico Roncuzzi](https://www.linkedin.com/in/enricoroncuzzi/), AI/ML Engineer, MSc Computer Science @ Politecnico di Milano.*
