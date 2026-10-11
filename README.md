# jd-scraper - grounded LLM pipeline for AI/ML job hunting

> A production pipeline that scrapes AI/ML job postings across four regions daily, verifies remote eligibility and scores fit with structured LLM output, stores everything in a Postgres corpus, and tailors a CV, cover letter, and recruiter message per posting, with every CV bullet taken from the real CV.

Built solo, test-driven, running unattended in production.

## The interesting part: grounded generation, enforced in code

The hard problem here isn't scraping, it's making an LLM produce text a hiring manager can trust. Two design choices do that:

- **Structured output everywhere an LLM touches the pipeline.** Scoring (`src/scorer.py`), remote-eligibility verification (`src/remote_verifier.py`), and CV tailoring (`src/tailor/generate.py`) each define a Pydantic schema and parse the model's response into it. No regex on free text, no brittle string parsing.
- **Anti-hallucination enforced at runtime, not by prompt wording.** `src/tailor/cv_master.py`'s `assemble()` never asks the LLM to write CV content: every bullet is included in canonical order, and only the skill groups are reordered, verbatim, from a hand-written canonical CV. The LLM only writes the cover-letter hook/bridge and the recruiter message. `src/tailor/validate.py` then byte-matches the assembled CV against six required metrics (e.g. `"94.1%"`, `"10.5M"`) and halts the run if one is missing. The cover letter is checked for banned phrases, length, and a concrete bridge. Hook claims are written to `CLAIMS_REVIEW.txt` for a person to confirm, rather than trusting the model to have been careful.
- **A Postgres corpus of scored, full-text job descriptions** (`src/storage.py`), the kind of ground-truth data set an eval or retrieval layer would need, growing daily from real production traffic rather than a scraped-once snapshot.

## What runs daily, zero-touch

1. **Scrape** - four region-scoped LinkedIn sweeps, paginated per query up to a per-tier page cap (`search.max_pages_per_query` in each tier config, resolved by `src/scraper.py`), where each page advances `start` by the number of cards the endpoint actually returned rather than by an assumed page size: tier 1 Italy full-remote, tier 2 Switzerland/San Marino any work mode, tier 3 EU/EEA full-remote, tier 4 United Kingdom full-remote. A tier narrows its results to the countries listed in its config's `search.allowed_countries` (enforced by `src/tier_scope.py`); tiers 1 and 4 list none and do no narrowing. Requests are paced, and a 429 widens the gap. Descriptions are kept when they are English, Spanish, or Italian (`src/language_filter.py`); very short text, and text the detector cannot classify, is kept too.
2. **Verify** - when the tier enables it, `src/remote_verifier.py` runs an LLM (OpenRouter `nvidia/nemotron-3-super-120b-a12b:free` primarily, falling back to Groq `openai/gpt-oss-20b` when that share is spent or a batch fails; the daily OpenRouter verification share is 4/10 of the allowance in `config/llm_limits.json`, 400 of 1000 requests, and only that stage is capped) over each description and rules it confirmed, rejected, or unconfirmed for genuine remote eligibility. Tier 2 leaves this off, because that sweep accepts any work mode. Verification runs before scoring, and every failure mode (no API key, an incomplete batch, an ambiguous description, both providers unavailable) resolves to unconfirmed instead of silently dropping a real job.
3. **Score** - every remaining posting is rated for fit against my profile by an LLM on OpenRouter (`nvidia/nemotron-3-super-120b-a12b:free`, with a 3-model native fallback array: `dots-studio/dots-3-note-preview:free`, `liquid/lfm-2.5-2.6b:free`, `apodex/apodex-1.1-mini:free`), returning structured Pydantic output with a one-line rationale. The shipped score cutoff is 8.
4. **Store** - every scored offer persists to a Neon Postgres corpus, full text included. Each run records telemetry for the tier, each search, and each LLM call (`src/telemetry.py`), which the morning report reads.
5. **Digest** - a ranked `digest.md` per tier lands in Obsidian, alongside a `rejected.md` audit trail of what verification screened out and why, plus a Telegram summary.
6. **Tailor** - one click on any offer in the digest runs the CV tailoring engine end to end, and the daily run does the same for offers that clear the cutoff. Each offer gets its own package directory.
7. **Morning health report** - when the four-tier cron run finishes, `orchestrator.py` sends four Telegram messages, one per tier, with a day summary at the end of the last. On the server, `python scripts/run_report.py day` prints the same view (`trend`, `compare`, and `llm` in `scripts/run_report.py`).

`LinkedIn -> scrape/dedup/filter -> remote verifier (OpenRouter, Groq fallback) -> LLM scorer (OpenRouter) -> Postgres corpus -> digest.md + Telegram -> [tailor] -> OpenRouter generation + validation gate -> PDF`

The scraper runs on a VPS via cron. Tailoring runs on demand, one click from the digest, and every tailored offer lands in its own directory.

## CV tailoring, working today

`tailor.py <job>` calls OpenRouter (`nvidia/nemotron-3-super-120b-a12b:free`) to reorder skill groups from a canonical CV for the specific job description. Every bullet is included, in canonical order. The model writes only the cover letter's hook/bridge and the recruiter message as free text. Everything else in the CV is copied byte-for-byte. The validation gate in `src/tailor/validate.py` aborts the run if a required metric is missing, or if the cover letter fails its checks for banned phrases, length, and a concrete bridge. Hook claims are written to `CLAIMS_REVIEW.txt` for a person to confirm. Headless Chromium then renders CV, cover letter, and recruiter message to PDF, reproducing the original template offline, and the CV must stay a single page. The package is one directory per offer.

## Tech stack

`Python` - `OpenRouter` & `Groq` LLM APIs - `Pydantic` - `PostgreSQL (Neon)` - `Playwright` (headless Chromium) - `LangChain` (thin scoring adapter over OpenRouter) - `pytest` (TDD)

## Engineering notes

- **Test-driven throughout**: `.venv/bin/python -m pytest tests/`, every module built red to green. Count them with `pytest tests/ --collect-only -q`; the number moves.
- **Structured LLM I/O everywhere**: Pydantic schemas for scoring, remote verification, and tailoring (skill order, cover letter, recruiter message), no string-parsed output.
- **No silent loss**: the dedup log records offers that were *handled*, not merely fetched. Whatever scoring never reached (a quota-exhausted or 5xx day) is written to a per-tier retry queue (`src/retry_queue.py`) with its description and remote verdict intact, re-scored ahead of fresh offers by the next run, expired after 3 days, and counted in the digest and Telegram summary. A description LinkedIn rate-limited is queued on that same path, refetched next run, and dropped after 2 days.
- **Resilience by design**: quota-aware exponential backoff (`src/retry.py`) and batch-level retry on 5xx/timeout, including OpenRouter's habit of surfacing an upstream 5xx as HTTP 200 with a JSON error body, distinguished in code (`_is_retryable_upstream_value_error` in `src/scorer.py`) from an unrelated bug before retrying. Late-stage notification calls are guarded so a Telegram outage degrades the message rather than the run: an uncaught error there used to re-trigger the whole tier, re-scraping and re-scoring up to four times.
- **Anti-hallucination in code, not just the prompt**: the CV is checked at runtime against the canonical source, not left to an instruction the model can ignore. The cover letter is checked for banned phrases, length, and a concrete bridge. Hook claims are listed in `CLAIMS_REVIEW.txt` for a person to confirm, not matched against the canonical CV.

## By the numbers

| Metric | Value | Verified via |
|---|---|---|
| Geographic tiers | **4** | `config/config_tier{1..4}.json` |
| Max pages per query | **16-100** per tier (~160-1000 cards at the endpoint's current 10/request); LinkedIn serves at most about 100 pages per search, so higher values have no effect | `search.max_pages_per_query` in `config/config_tier{1..4}.json` |
| Pipeline source lines (`main.py`, `orchestrator.py`, `src/`) | **~5,800** | `wc -l` |
| Manual steps in the daily run | **0** | cron-driven, see `AGENTS.md` |

## Run it

```bash
pip install -r requirements.txt
playwright install chromium
cp .env.template .env
```

Set `LLM_API_KEY`, `GROQ_API_KEY`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `DATABASE_URL`, and `OUTPUT_PATH`. Tailoring also needs `CV_MASTER_PATH`, `CV_CSS_PATH`, and `JD_OUTPUT_ROOT`. The CV source files are not in this repo.

```bash
python orchestrator.py
python main.py config/config_tier1.json
python tailor.py path/to/note.md
python -m pytest tests/
```

## Roadmap

- **Hybrid RAG** (BM25 + dense + reranker) over the corpus and my profile.
- **Eval framework**: LLM-as-judge, retrieval metrics (recall@k, MRR), and an honest write-up of where it wins and loses.
- **Production layer**: a FastAPI service, Docker, and an observability dashboard. Test CI and per-run telemetry are already in place.

---

*A personal tool: local paths and CV source content are wired to my own machine, so the repo is here primarily to read. Built and maintained by [Enrico Roncuzzi](https://www.linkedin.com/in/enricoroncuzzi/), AI/ML Engineer, MSc Computer Science @ Politecnico di Milano.*
