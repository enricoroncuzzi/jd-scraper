# Phase 4 — Autonomous Application Agent (design)

**Date:** 2026-07-29
**Author:** Enrico Roncuzzi
**Status:** Design approved, not yet implemented
**Supersedes phase numbering:** old Phase 4→5, 5→6, 6→7, 7→8
**Depends on:** Phase 1 (scraper), Phase 2 (VPS + Neon corpus), Phase 3 (CV/CL/HR generator)

---

## 1. Goal

Add an automation layer that **submits real job applications on my behalf**, triggered by a
button next to an offer in the daily digest, running unattended on the Netcup VPS, and
reporting back exactly what it did.

**v1 (this phase):** human-selected. I read the digest, decide I like an offer, press
`[📮 apply]`, and the agent tailors + applies + reports.

**v2 (future, out of scope here):** the same code path triggered by cron instead of by me —
a fully automatic daily application loop. The architecture below is built so that v2 is a
*configuration change*, not a rewrite.

---

## 2. Decisions taken (and rejected alternatives)

| # | Decision | Chosen | Rejected |
|---|---|---|---|
| 1 | Application scope | **External ATS + LinkedIn Easy Apply** | ATS-only (safer but partial coverage) |
| 2 | Execution topology | **Full pipeline server-side on the VPS** | Tailor on Mac + rsync artifacts; all-local |
| 3 | Unanswerable form question | **Pause, ask me, resume via replay** | Halt-and-escalate; let the LLM invent an answer |
| 4 | Go-live safety | **Staged rollout: shadow mode → live** | Straight to live; permanent per-application approval |
| 5 | Control model | **Deterministic-first + LLM fallback + cache** | Fully LLM-driven agent; fully hand-written scripts |

Decision 1 carries real risk (see §9) and was taken with that risk stated explicitly.

---

## 3. Technology choice, grounded in the corpus

Frequencies below are **document counts across the 19,975 scraped JD notes** in
`work/jd-output/` — i.e. what the EU AI/ML market is actually asking for, from my own data.

| Technology | Docs | Role in this phase |
|---|---|---|
| agentic / ai agent | 4,243 / 2,738 | the dominant market theme this phase targets |
| **LangGraph** | 1,107 | state-machine orchestration of the apply flow |
| **MCP** (+ protocol) | 1,142 / 359 | optional later: expose apply tools over MCP |
| observability | 2,748 | cost/latency ledger feeds the Phase 7 dashboard |
| guardrail | 853 | pre-submit verification gate |
| **human-in-the-loop** | 349 | the pause/resume and shadow-mode design *is* HITL |
| **Playwright** | 318 | browser automation — **already a repo dependency** |

Two deliberate reuses: **Playwright** is already installed for the Phase 3 Chromium PDF
render, and **`src/telegram.py`** already exists for notifications.

LangGraph is a considered exception to the roadmap's "no LangChain in the RAG system" rule.
That rule protects the *retrieval* stack, which stays hand-rolled. Orchestrating a
multi-step, resumable, human-in-the-loop browser workflow is exactly LangGraph's purpose,
and it is the single most-requested agentic framework in the corpus.

---

## 4. Architecture

```
 [📮 apply] link in a digest note   (Mac — next to the existing [🎯 tailor] link)
        │
        │  ssh root@vps  →  enqueue(offer_id)
        ▼
 ┌────────────────────────────────────────────────┐
 │ apply_jobs queue  (Neon Postgres)              │  ← v2: cron enqueues instead of me
 └───────────────────┬────────────────────────────┘
                     │  worker polls
                     ▼
 ┌────────────────────────────────────────────────────────────────┐
 │ LangGraph state machine — Netcup VPS                           │
 │                                                                │
 │  1. tailor            reuse src/tailor/  → CV.pdf + CL.pdf     │
 │  2. resolve_apply_url open LinkedIn note link                  │
 │                       → Easy Apply | external redirect         │
 │  3. classify_ats      URL + DOM fingerprint          0 tokens  │
 │  4. fill_form         known ATS → adapter            0 tokens  │
 │                       unknown   → LLM maps fields → CACHED     │
 │  5. answer_questions  answer_bank.yaml               0 tokens  │
 │                       unknown → PAUSE (§6)                     │
 │  6. verify            pre-submit guardrail gate                │
 │  7. submit            shadow: skip | live: click               │
 │  8. report            Telegram + APPLY_REPORT.md               │
 └────────────────────────────────────────────────────────────────┘
```

**Why SSH-enqueue rather than an HTTP endpoint:** no new public attack surface on the VPS,
and it reuses the existing `ssh + git pull` deploy path. The queue table is what makes v2
trivial — cron enqueues rows instead of me clicking.

**Why the queue at all:** it decouples trigger from execution, survives VPS reboots, gives
each application a durable state record, and serialises runs so the agent never drives a
browser concurrently with the scraper (same IP — see §9).

---

## 5. Components

### 5.1 Trigger (`[📮 apply]`)
Mirrors the existing Phase 3 `[🎯 tailor]` machinery in `src/writer.py`: an inline link on
high-score digest offers, a macOS URL-scheme handler app, path-traversal guarded. Instead of
running locally it opens an SSH connection and enqueues the offer.

### 5.2 Queue + worker
`apply_jobs` table. States: `queued → running → (awaiting_answer | shadow_complete |
submitted | failed)`. Single worker, one job at a time.

### 5.3 ATS classifier
Deterministic: URL pattern + DOM fingerprint → `greenhouse | lever | ashby | workday |
personio | recruitee | smartrecruiters | linkedin_easy_apply | unknown`. Zero tokens.

### 5.4 Form adapters
One deterministic Playwright adapter per known ATS. Greenhouse/Lever/Ashby are stable and
simple; Workday/Taleo are multi-step and account-gated — explicitly **best-effort, expected
to escalate often**.

### 5.5 Unknown-form LLM fallback + cache
On `unknown`, the LLM receives a **pruned** form schema (field labels, types, `name`
attributes — never raw page HTML) and returns a field→value mapping. That mapping is written
to `ats_field_cache/<fingerprint>.json`. **The next application on that ATS costs zero
tokens.** Cost decays with usage instead of scaling linearly.

### 5.6 Answer bank
`config/answer_bank.yaml` — canonical facts: visa status, notice period, salary range,
per-technology years of experience, languages, demographics (optional fields).

**Hard rule: the LLM never authors a factual answer.** This extends the Phase 3
anti-fabrication guarantee — a fabricated fact in a submitted application is materially
worse than a bad CV bullet. Free-text questions ("why this company?") are *not* factual
claims and may be drafted by the LLM, grounded in the existing cover-letter machinery.

Question matching is normalised + fuzzy, so trivial rewordings hit the same entry.

### 5.7 Pre-submit verification gate
Blocks submission unless: all required fields non-empty · CV + CL attached · no placeholder
or template text · no `{company}`-style unfilled tokens · **not a duplicate** (company+role
checked against the `applications` table) · token budget not exceeded.

### 5.8 Reporting
Per run: Telegram summary + `APPLY_REPORT.md` in the artifact folder containing the step
trace, ATS detected, every field filled, every question answered and its source
(bank/LLM/me), screenshots (incl. the completed pre-submit form), token/call/€ cost, final
state, and confirmation evidence.

---

## 6. Pause and resume (decision 3) — replay, not frozen sessions

Naive resume (hold the browser open and wait) is fragile: sessions expire, memory leaks, the
VPS reboots. Instead:

1. Agent hits a question absent from the answer bank.
2. Persists pending question + context, screenshots it, **closes the browser cleanly**.
3. State → `awaiting_answer`; Telegram message asks me the question.
4. I reply; the answer is written into `answer_bank.yaml`.
5. Agent **replays the application from the start** with the enriched bank.

Replay is safe precisely because the run halted *before* submitting, and idempotent because
every factual field is filled deterministically from the bank. Each replay gets further. The
bank converges — after ~20 applications new questions become rare.

---

## 7. Cost control (explicit requirement)

| Mechanism | Effect |
|---|---|
| Deterministic classification + adapters | 0 tokens on the common path |
| Answer-bank lookups | 0 tokens for every factual field |
| LLM scoped to unknown-form mapping + free text | narrow, bounded surface |
| Pruned form schema (never raw HTML) to the LLM | small prompts |
| **Per-ATS field-mapping cache** | unknown ATS costs tokens **once**, then 0 |
| Per-application ceiling: ~15k tokens / max 6 calls | exceeded → halt, no submit |
| Daily global token budget + max applications/day | bounds the worst case absolutely |
| Groq `gpt-oss-120b` only (no frontier model) | already the Phase 3 generation stack |
| `apply_costs` ledger + `usage_log.jsonl` extension | per-application tokens/calls/latency/€ |

The ledger is deliberately the same shape as the existing scraper token tracking, so it
feeds the Phase 7 observability dashboard with no rework.

---

## 8. Data model

| Store | Purpose |
|---|---|
| `apply_jobs` | queue + state machine + attempt count |
| `applications` | dedup ledger — never apply twice to the same company+role |
| `apply_costs` | tokens / calls / latency / € per application |
| `config/answer_bank.yaml` | canonical facts + learned Q→A pairs |
| `ats_field_cache/` | per-ATS field mappings (the cost-decay mechanism) |
| artifact folder | CV, CL, screenshots, `APPLY_REPORT.md` |

`applications` also delivers the application-tracking (applied/interview/rejected) that
Phase 2 Track B deferred.

---

## 9. Risks

| Risk | Mitigation |
|---|---|
| **LinkedIn account ban** (decision 1 — accepted risk). Automating Easy Apply violates ToS, and the same account is the scraper's data source. | Persistent logged-in profile (cookie reuse, never scripted logins) · randomised human-paced delays · hard cap ~5 Easy Apply/day · **never concurrent with the scraper** (reuses the orchestrator's sequential + cooldown pattern) · env kill switch disabling all LinkedIn interaction · dedicated application email as contact |
| **Irreversible bad submission** | Shadow mode first (decision 4) + verification gate (§5.7) + dedup. Nothing submits until I have inspected N shadow runs and flipped the flag |
| **CAPTCHA / anti-bot wall** — the real coverage ceiling; not legitimately solvable | Detect → halt → report. Honest expectation: partial ATS coverage, never universal |
| **Fabricated answer in a real application** | Answer bank is the only source of factual answers; LLM structurally cannot author them |
| **Runaway token cost** | Per-application and daily ceilings (§7); breach halts before submit |
| **Workday/Taleo complexity sinks the phase** | Explicitly best-effort. Definition of done covers Greenhouse/Lever/Ashby + Easy Apply only |
| **Scope creep into full autonomy too early** | v2 (cron loop) is out of scope. Ship button-triggered first |
| **This displaces RAG (now Phase 5), already the schedule slip** | Conscious trade: agentic (4,243 docs) outsignals RAG (11,774 but commoditised in portfolios) and delivers immediate value. Accepted |

---

## 10. Testing (TDD, RED → GREEN as in Phases 1–3)

- Saved **HTML fixtures per ATS** → adapter tests with zero network calls
- State-machine tests with a mocked browser (every transition + every halt path)
- **Shadow-mode test asserting `submit()` is never called**
- Budget-enforcement tests (per-application and daily ceilings trigger halt)
- Answer-bank fuzzy-matching tests, including the unknown → pause path
- Replay idempotency test (same inputs + enriched bank → further progress, no double submit)
- Dedup test (second application to same company+role is blocked)
- Verification-gate tests for each blocking condition

---

## 11. Definition of done

- [ ] `[📮 apply]` on a digest offer enqueues a job on the VPS
- [ ] Agent tailors, detects the ATS, fills the form, and produces a complete `APPLY_REPORT.md`
- [ ] **Shadow mode** verified on ≥5 real offers across ≥2 ATS families
- [ ] Unknown question → Telegram → my reply → successful replay, end to end
- [ ] Cost ledger populated; per-application and daily ceilings enforced and tested
- [ ] Dedup prevents a second application to the same company+role
- [ ] Live mode flipped on; **≥1 real application submitted autonomously** with confirmation captured
- [ ] Greenhouse + Lever + Ashby + LinkedIn Easy Apply supported; everything else halts cleanly

---

## 12. Out of scope

Fully automatic daily loop (v2) · CAPTCHA solving · Workday/Taleo guaranteed support ·
outreach messaging (Phase 6) · anything RAG (Phase 5).
