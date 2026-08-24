# jd-scraper — an AI job-hunt pipeline

> An end-to-end system that scrapes EU-remote AI/ML jobs every day, scores each one with an LLM, and turns any high-fit posting into a **tailored one-page CV + cover letter + recruiter message** with a single click.

Built solo, test-driven, and running unattended in production.

Job hunting for AI/ML roles is two problems at once: a **data problem** (finding the right roles in the noise) and a **writing problem** (tailoring each application well, at scale). This project automates both — a scraper that surfaces the right roles, and a *grounded* LLM pipeline that rewrites my application for each job **without inventing anything that isn't on my real CV**.

---

## What it does (daily, zero-touch)

1. **Scrape** — a 4-tier geographic sweep of LinkedIn for remote/hybrid AI/ML roles across the EU.
2. **Score** — every posting rated 1–10 for fit against my profile by an LLM, with a one-line rationale.
3. **Store** — each scored offer persisted to a Postgres corpus (4,000+ offers and growing).
4. **Digest** — a ranked daily digest lands in my notes (plus a Telegram summary).
5. **Tailor** — click a 🎯 link next to any offer → a CV, cover letter, and recruiter message (3 PDFs) in ~40 s.

```
 LinkedIn (4 tiers)
        │  requests + BeautifulSoup
        ▼
 Scraper ─ dedup ─ language filter
        │
        ▼
 LLM scorer (OpenRouter, structured output)  ──►  Postgres corpus (Neon)
        │                                              (full text + scores)
        ▼
 Daily digest (Obsidian)  +  Telegram summary
        │
        │  click  [🎯 tailor]
        ▼
 CV tailoring engine (Groq, verbatim + validated)
        │  → headless-Chromium PDF render
        ▼
 tailored CV · cover letter · recruiter message
```

The scraper runs on a VPS via cron; the tailoring runs locally, one click from the digest.

---

## Three subsystems

### 1 · Scraper + scoring + corpus  *(in production)*
- **4-tier scraper** (Italy/Spain → Western EU → UK/CH → Eastern EU), config-driven per tier.
- **LLM scoring** via OpenRouter (free-tier models, with a native fallback array across 3 alternates) with **structured Pydantic output** — not string-parsing.
- Per-tier **dedup**, language filtering, **exponential-backoff retries**, and **partial-save on quota exhaustion** (a daily run never loses completed work).
- **Neon Postgres corpus** (full descriptions + scores) — the data foundation for the retrieval/eval work on the roadmap.
- Deployed on a **Netcup VPS** via a sequential-tier cron orchestrator — unattended.

### 2 · CV tailoring engine + one-click button
- `tailor.py <job>` → **Groq** (`openai/gpt-oss-120b`) selects and reorders verbatim bullets/skills from my canonical CV for the specific JD, and writes only the cover-letter hook/bridge and the recruiter message as free text.
- **No hallucination by construction** — the CV body is never paraphrased, only selected/reordered from hand-written text; a validation gate byte-matches the assembled CV against required metrics and checks cover-letter claims, **aborting** on any mismatch.
- **Identical layout** — only the summary + experience bullets are selected/reordered; everything else is copied *byte-for-byte*, so the tailored CV renders to the **same single A4 page** as the original.
- **Faithful PDF** — headless Chromium reproduces my exact résumé template (fonts, columns, scale-to-fit) offline; the output is visually indistinguishable from my real CV.
- **One click** — a custom macOS URL handler runs the whole pipeline straight from a link in the digest.

### 3 · Auto-apply v1 (draft-and-notify, opt-in)
- Off by default (`config.autoapply.enabled: false`) — the production tier configs don't enable it, so the daily run stays zero-touch as described above unless I opt in.
- When enabled: classifies each above-threshold offer's application channel with a read-only HTTP GET (no browser automation, no logins), auto-runs the same tailoring pipeline, and packages the result with a notification for me to review and submit **manually** — there is no auto-submit path.
- See `AGENTS.md` for the full design and the storage/config wiring.

---

## Tech stack

`Python` · `OpenRouter` & `Groq` LLM APIs · `Pydantic` · `PostgreSQL (Neon)` · `Playwright` (headless Chromium) · `LangChain` (thin scoring adapter) · `pytest` (TDD) · `Netcup VPS` + `cron`

---

## Engineering notes

- **Test-driven throughout** — 150 tests, every module built RED → GREEN.
- **Structured LLM I/O** — Pydantic schemas for both scoring and generation; no brittle output parsing.
- **Resilience by design** — capped exponential backoff + partial-save mean rate limits degrade gracefully instead of dropping a run.
- **Anti-hallucination in code, not just the prompt** — the "every claim traces to the source CV" guarantee is enforced by a runtime check.
- **Deterministic, layout-preserving rewrite** — in-place substring substitution keeps the non-rewritten parts of the CV identical, so length and formatting can't drift.

## By the numbers

| | |
|---|---|
| Scored offers in the corpus | **4,000+** (2+ weeks of clean daily data) |
| Geographic tiers | **4** (~500–700 offers scored/day) |
| Automated tests | **~130** |
| Manual steps in the daily run | **0** |
| Time to a tailored application | **~40 s** |

## Roadmap

- **Hybrid RAG** (BM25 + dense + reranker) over the corpus and my profile.
- **Eval framework** — LLM-as-judge, retrieval metrics (recall@k, MRR), baselines to beat, and an honest write-up of where it wins and loses.
- **Production layer** — FastAPI + Docker + CI, and an observability dashboard (cost / latency / hit-rate).

---

*A personal tool: local paths and the CV source are wired to my own machine, so the repo is here primarily to **read**. Built and maintained by [Enrico Roncuzzi](https://www.linkedin.com/in/enricoroncuzzi/) — AI/ML Engineer, MSc Computer Science @ Politecnico di Milano.*
