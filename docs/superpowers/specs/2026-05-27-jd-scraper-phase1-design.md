# jd-scraper Phase 1 — Design Spec

**Date:** 2026-05-27
**Author:** Enrico Roncuzzi
**Status:** Approved

---

## Overview

A LinkedIn job scraper that runs daily, scores offers against a candidate profile using a free LLM, writes structured notes to an Obsidian vault, and sends a Telegram summary. Phase 1 is a local, stateful MVP — no AWS, no CV customization, no RAG.

**Out of scope for Phase 1:** CV generation, AWS Lambda/EventBridge, RAG pipeline, multi-search queries.

---

## Section 1 — Project Structure

```
jd-scraper/
├── main.py                    ← handler(event, context) + __main__ entry
├── src/
│   ├── __init__.py
│   ├── models.py              ← Pydantic models
│   ├── scraper.py             ← LinkedIn guest API + BeautifulSoup
│   ├── scorer.py              ← LangChain + Groq
│   ├── dedup.py               ← link hash log
│   ├── obsidian.py            ← vault writer
│   └── telegram.py            ← summary sender
├── config/
│   ├── config.example.json    ← committed template
│   └── config.json            ← gitignored, actual values
├── data/
│   └── .gitkeep               ← seen_offers.txt lives here, gitignored
├── docs/
│   └── superpowers/specs/     ← design docs
├── .env.template              ← committed, no values
├── .env                       ← gitignored, actual secrets
├── .gitignore
├── requirements.txt
└── README.md
```

`src/` is a proper Python package (importable via `from src.scraper import ...`). `main.py` sits at root to match the AWS Lambda handler pattern (`main.handler`).

---

## Section 2 — Data Models

All internal data flows through Pydantic models. Optional fields handle malformed LinkedIn offers (missing location, empty description).

```python
# src/models.py

class JobOffer(BaseModel):
    id: int
    title: str
    company: str
    location: str = "N/A"       # missing on some LinkedIn offers
    link: str
    description: str = ""       # empty if LinkedIn blocks description fetch

class ScoredOffer(JobOffer):    # inherits all JobOffer fields
    score: int = Field(ge=1, le=10)
    comment: str = ""
    summary: str = ""

class RankedOffers(BaseModel):  # structured output envelope for LangChain
    offers: list[ScoredOffer]
```

**Malformed offer handling:** Groq prompt instructs the model to score offers with empty descriptions as `score=1`, `comment="Description unavailable — could not evaluate."`. All offers (including malformed) are written to the vault and marked seen.

---

## Section 3 — Configuration Schema

Two config files: `.env` for secrets (never committed), `config.json` for pipeline behavior (committed as `config.example.json`).

**`.env`:**
```
GROQ_API_KEY=
TELEGRAM_TOKEN=
TELEGRAM_CHAT_ID=
OBSIDIAN_VAULT_PATH=/Users/enricoroncuzzi/Desktop/raw/work
DEDUP_LOG_PATH=/Users/enricoroncuzzi/Desktop/raw/projects/jd-scraper/data/seen_offers.txt
```

**`config.json`:**
```json
{
  "search": {
    "role": "AI Engineer",
    "location": "Europe",
    "time_range": "r86400"
  },
  "scoring": {
    "threshold": 8,
    "exclude_keywords": ["Principal", "Staff", "VP", "Director",
                         "10+ years", "8+ years", "C++", "Rust"],
    "priority_keywords": ["LLM", "RAG", "remote", "FastAPI",
                          "Docker", "PyTorch", "HuggingFace"],
    "candidate_profile": "Junior ML/AI engineer, MSc Polimi, strong in Python, LLMs, RAG pipelines. Targeting remote EU roles. Experience: LangChain, HuggingFace, FastAPI, Docker."
  },
  "telegram": {
    "greeting": "Hey Enrico!"
  }
}
```

`threshold` in `config.json` (not `.env`) because it is pipeline behavior, not a secret. Single `search{}` object (not an array) — YAGNI.

---

## Section 4 — Error Handling

**LinkedIn scrape failures:**
- If the guest API returns non-200: log the status code, raise `RuntimeError`, abort the run. Do not write partial results.
- If a per-offer description fetch fails (non-200 or timeout): set `description=""`, continue. The offer is scored as malformed (score=1, comment="Description unavailable").
- Timeouts: 10s per description fetch request.

**Groq API failures:**
- If the scoring call fails (rate limit, network error): log the error and abort the run. Do not write partial results to the vault.
- Groq free tier limits: 1K RPD, 100K TPD. A run scoring 20 offers uses ~3-5K tokens — well within limits.

**Dedup state:**
- `seen_offers.txt` is written only after a fully successful run (all stages complete). A partial run leaves dedup state unchanged — the next run re-processes the same offers.
- If `seen_offers.txt` does not exist, treat as empty (first run).

**Obsidian / Telegram failures:**
- If vault write fails: log, continue to next offer, send Telegram at end.
- If Telegram send fails: log, do not abort — vault notes are the primary output.

---

## Section 5 — Obsidian Output Format

Two output types per run.

**Per-JD note** — one file per scored offer (all offers, including malformed):

Path: `{OBSIDIAN_VAULT_PATH}/jobs/scraped/YYYY-MM-DD_{company}_{title}.md`

```markdown
---
date: 2026-05-27
score: 8
company: Some Company
location: Milan, Italy
link: https://linkedin.com/jobs/view/...
tags: [job, scraped, high-score]
---

# AI Engineer — Some Company

**Location:** Milan, Italy
**Score:** 8/10
**Comment:** Strong RAG and LLM focus, remote-friendly, aligns with target stack.
**Summary:** Seeking AI engineer to build production RAG pipelines. Stack: Python, FastAPI, LangChain, AWS.
**Link:** https://linkedin.com/jobs/view/...
**Scraped:** 2026-05-27

## Job Description

<full description text>
```

Tags: `high-score` if `score >= threshold`, `low-score` otherwise. File name: spaces → underscores, lowercase, non-alphanumeric chars stripped, company and title each truncated to 40 chars.

**Daily digest** — one file per run:

Path: `{OBSIDIAN_VAULT_PATH}/jobs/digest/YYYY-MM-DD_digest.md`

```markdown
# Job Digest — 2026-05-27

## High-Score Offers (≥8)

- **AI Engineer — Some Company** (9/10) · Milan · [link]
  Strong RAG focus, remote, FastAPI stack.

## Low-Score Offers

3 offers below threshold. Notes written to jobs/scraped/.
```

If no new offers after dedup: `No new offers after dedup filter.`

---

## Section 6 — Data Flow

```
scraper.py  → list[JobOffer]      LinkedIn guest API + BeautifulSoup (two-pass)
     ↓
dedup.py    → list[JobOffer]      filter already-seen by link hash (MD5)
     ↓
scorer.py   → list[ScoredOffer]   Groq llama-3.3-70b-versatile via LangChain
     ↓
main.py     → high/low split      threshold from config.json
     ↓
obsidian.py → vault notes         per-JD note + daily digest
     ↓
telegram.py → summary message     high-score list + low count
     ↓
dedup.py    → mark all seen       only after full successful run
```

No shared state between modules. Each module receives inputs and returns outputs. `main.py` is the only orchestrator.

---

## Section 7 — LLM Integration

**Provider:** Groq (`langchain-groq`, `ChatGroq`)
**Model:** `llama-3.3-70b-versatile`
**Structured output:** LangChain `.with_structured_output(RankedOffers, method="function_calling")`

Scoring prompt sends all new offers in one batch (no chunking in v1 — YAGNI). Prompt includes:
- `candidate_profile` from config
- `priority_keywords` and `exclude_keywords` from config
- All offer titles + descriptions
- Instruction: score 1-10, score empty descriptions as 1 with "Description unavailable" comment

**Chain pattern:**
```python
chain = (
    ChatPromptTemplate.from_messages([...])
    | ChatGroq(model="llama-3.3-70b-versatile").with_structured_output(RankedOffers)
)
result = chain.invoke({"offers": offers, "profile": config.candidate_profile, ...})
```

---

## Section 8 — Local Run & Scheduling

**Local run:**
```bash
python main.py
```

`main.py` calls `handler({}, None)` — same entry point as Lambda (Phase 5).

**Scheduling (macOS cron):**
```
0 8 * * 1-5 cd /path/to/jd-scraper && source .venv/bin/activate && python main.py
```
Weekdays at 08:00. `crontab -e` to set.

**Lambda migration (Phase 5):** zip package + IAM role + environment variables in Lambda console. No code changes required — `handler(event, context)` is already the entry point.

---

## Dependencies

```
requests
beautifulsoup4
langchain-groq
langchain-core
pydantic
python-dotenv
```

Python 3.11+. All free, no credit card required.
