"""Run telemetry: one record per tier attempt, per search, and per LLM call.

Records are appended to a local write-ahead buffer (one JSON object per line
under buffer_dir()) and flushed to Neon in one transaction at tier start and
tier end. Every record carries a client-generated UUID, so re-flushing a buffer
- after a crash, or a Neon outage - never duplicates rows. Neon stays the single
source of truth: a buffer file is deleted as soon as it has been flushed.

Nothing here may ever raise into pipeline code. Every public entry point is
wrapped; a failure prints one warning per failure site and marks the session's
telemetry_ok false, which the morning report surfaces.

Design: data/run-observability/design.md in the firstmate home.
"""
import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

import groq
import openai
import psycopg2
from pydantic import ValidationError

from src import storage

STOP_REASONS = ("exhausted_underfull", "empty_page", "duplicate_page",
                "end_of_results", "cap_hit", "error")

_RUN_OPEN_COLUMNS = ("run_uuid", "run_at", "tier", "daily_run_id", "attempt",
                     "git_commit", "git_dirty", "status", "started_at")
_RUN_CLOSE_COLUMNS = (
    "status", "finished_at", "error", "offers_fetched", "offers_new",
    "offers_deferred", "offers_packaged", "prompt_tokens", "completion_tokens",
    "total_tokens", "verification_provider", "verification_tokens",
    "verification_batches_failed", "verification_batches_total",
    "verification_degraded", "verification_confirmed", "verification_unconfirmed",
    "verification_rejected", "search_rate_limits", "description_rate_limits",
    "telemetry_ok",
)
_QUERY_COLUMNS = ("id", "run_uuid", "role", "location", "work_mode", "pages_walked",
                  "page_cap", "cards_seen", "offers_kept", "cross_query_duplicates",
                  "stop_reason", "recorded_at")
_LLM_COLUMNS = ("id", "run_uuid", "stage", "provider", "request_model", "response_model",
                "input_tokens", "output_tokens", "latency_ms", "batch_size", "attempt",
                "outcome", "error", "prompt_version", "started_at")

_current = None
_warned_sites: set[str] = set()


def _reset_for_tests() -> None:
    global _current
    _current = None
    _warned_sites.clear()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def buffer_dir() -> str:
    return os.environ.get("JDS_TELEMETRY_DIR", "data/telemetry")


def _redact(text: str) -> str:
    text = re.sub(r"postgres(?:ql)?://\S+", "[redacted]", text)
    return re.sub(r"/bot\d+:[\w-]+", "/bot[redacted]", text)


def _warn(site: str, exc: BaseException) -> None:
    if _current is not None:
        _current.telemetry_ok = False
    if site not in _warned_sites:
        _warned_sites.add(site)
        print(f"[telemetry] {site} failed ({type(exc).__name__}: {_redact(str(exc))}) - the run "
              f"continues; today's record may be incomplete.")


def _safe(site: str, fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        _warn(site, exc)
        return None


@dataclass(frozen=True)
class GitInfo:
    commit: str
    dirty: bool


def read_git_info(repo_dir: str = ".") -> GitInfo:
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir,
                              capture_output=True, text=True, timeout=5)
        if head.returncode != 0 or len(head.stdout.strip()) != 40:
            return GitInfo("unknown", False)
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                                cwd=repo_dir, capture_output=True, text=True, timeout=5)
        dirty = status.returncode == 0 and bool(status.stdout.strip())
        return GitInfo(head.stdout.strip(), dirty)
    except Exception:
        return GitInfo("unknown", False)


def prompt_version(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:12]


def _insert(cur, table: str, columns: tuple, data: dict, conflict: str) -> None:
    placeholders = ", ".join(["%s"] * len(columns))
    cur.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({conflict}) DO NOTHING",
        [data.get(c) for c in columns],
    )


def _connect(db_url: str):
    """Bound an observability connection so a dead database cannot stall a run."""
    return psycopg2.connect(
        db_url,
        connect_timeout=10,
        options="-c statement_timeout=15000 -c lock_timeout=5000",
    )


def flush_records(records: list[dict], db_url: str) -> None:
    """Write records in order, in one transaction. Raises on any failure."""
    conn = _connect(db_url)
    try:
        with conn.cursor() as cur:
            for record in records:
                kind, data = record.get("kind"), record.get("data") or {}
                if kind == "run_open":
                    _insert(cur, "runs", _RUN_OPEN_COLUMNS, data, "run_uuid")
                elif kind == "run_close":
                    assignments = ", ".join(f"{c} = %s" for c in _RUN_CLOSE_COLUMNS)
                    cur.execute(f"UPDATE runs SET {assignments} WHERE run_uuid = %s",
                                [data.get(c) for c in _RUN_CLOSE_COLUMNS] + [data.get("run_uuid")])
                elif kind == "query":
                    _insert(cur, "run_queries", _QUERY_COLUMNS, data, "id")
                elif kind == "llm_call":
                    _insert(cur, "llm_calls", _LLM_COLUMNS, data, "id")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _read_buffer(path: str) -> list[dict]:
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"[telemetry] skipping a torn line in {path}")
    return records


def _persist_incomplete_close(path: str) -> None:
    """Rewrite a buffered run_close so a failed flush cannot persist telemetry_ok true."""
    if not os.path.exists(path):
        return
    try:
        records = _read_buffer(path)
    except Exception as exc:
        _warn("close rewrite", exc)
        return
    changed = False
    for record in records:
        if record.get("kind") != "run_close":
            continue
        data = record.setdefault("data", {})
        if data.get("telemetry_ok") is not False:
            data["telemetry_ok"] = False
            changed = True
    if not changed:
        return
    try:
        with open(path, "w") as f:
            for record in records:
                f.write(json.dumps(record) + "\n")
    except Exception as exc:
        _warn("close rewrite", exc)


def flush_buffer_file(path: str, db_url: str) -> bool:
    if not os.path.exists(path):
        return True
    try:
        records = _read_buffer(path)
        if records:
            flush_records(records, db_url)
        os.remove(path)
        return True
    except psycopg2.OperationalError as exc:
        _warn("flush", exc)
        raise
    except Exception as exc:
        _warn("flush", exc)
        return False


def drain_buffers(buffer_dir: str, db_url: str, *, exclude: str | None = None) -> None:
    try:
        if not os.path.isdir(buffer_dir):
            return
        excluded = os.path.abspath(exclude) if exclude else None
        for name in sorted(os.listdir(buffer_dir)):
            path = os.path.join(buffer_dir, name)
            if not name.endswith(".jsonl") or os.path.abspath(path) == excluded:
                continue
            try:
                flush_buffer_file(path, db_url)
            except psycopg2.OperationalError:
                # One dead connection is enough. Leave the remaining files
                # for a later drain instead of waiting out a timeout each.
                return
    except Exception as exc:
        _warn("drain", exc)


def _lookup_run_id(db_url: str, run_uuid: str) -> int | None:
    conn = _connect(db_url)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM runs WHERE run_uuid = %s", (run_uuid,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


class TierSession:
    def __init__(self, *, tier, daily_run_id, attempt, db_url, buffer_dir, git):
        self.enabled = bool(db_url)
        self.db_url = db_url
        self.tier = tier
        self.daily_run_id = daily_run_id
        self.attempt = attempt
        self.git = git
        self.run_uuid = str(uuid.uuid4())
        self.run_id = None
        self.buffer_dir = buffer_dir
        self.buffer_path = os.path.join(buffer_dir, f"{self.run_uuid}.jsonl")
        self.started_at = _utcnow()
        self.fields: dict = {}
        self.counters = {"search_rate_limits": 0, "description_rate_limits": 0}
        self.telemetry_ok = True

    def _append(self, kind: str, data: dict) -> None:
        os.makedirs(self.buffer_dir, exist_ok=True)
        with open(self.buffer_path, "a") as f:
            f.write(json.dumps({"kind": kind, "data": data}) + "\n")

    def _flush_and_lookup(self) -> None:
        if flush_buffer_file(self.buffer_path, self.db_url):
            self.run_id = _safe("run id lookup", _lookup_run_id, self.db_url, self.run_uuid)

    def open(self) -> None:
        data = {
            "run_uuid": self.run_uuid, "run_at": self.started_at, "tier": self.tier,
            "daily_run_id": self.daily_run_id, "attempt": self.attempt,
            "git_commit": self.git.commit, "git_dirty": self.git.dirty,
            "status": "running", "started_at": self.started_at,
        }
        try:
            self._append("run_open", data)
        except Exception as exc:
            # The local buffer could not take the row. Neon may still be up,
            # and offer persistence is gated on this run id, so insert directly.
            _warn("open buffer", exc)
            _safe("open direct", flush_records, [{"kind": "run_open", "data": data}], self.db_url)
            self.run_id = _safe("run id lookup", _lookup_run_id, self.db_url, self.run_uuid)
            return
        self._flush_and_lookup()

    def ensure_run_id(self) -> int | None:
        if self.enabled and self.run_id is None:
            _safe("run id retry", self._flush_and_lookup)
        return self.run_id

    def close(self, status: str, error: str | None) -> None:
        self._append("run_close", {
            "run_uuid": self.run_uuid, "status": status, "finished_at": _utcnow(),
            "error": error[:500] if error else None,
            **self.fields, **self.counters, "telemetry_ok": self.telemetry_ok,
        })
        try:
            flushed = flush_buffer_file(self.buffer_path, self.db_url)
        except psycopg2.OperationalError:
            flushed = False
        if not flushed:
            # The line just written still says telemetry_ok true. A later drain
            # would persist that and the morning report would miss the gap.
            _persist_incomplete_close(self.buffer_path)


def start_session(*, tier: int, daily_run_id: str, attempt: int, db_url: str | None,
                  buffer_directory: str | None = None, repo_dir: str = ".") -> TierSession:
    global _current
    session = TierSession(tier=tier, daily_run_id=daily_run_id, attempt=attempt,
                          db_url=db_url, buffer_dir=buffer_directory or buffer_dir(),
                          git=read_git_info(repo_dir))
    _current = session
    if session.enabled:
        _safe("init_db", storage.init_db, db_url, connect_timeout=10)
        drain_buffers(session.buffer_dir, db_url, exclude=session.buffer_path)
        _safe("open", session.open)
    return session


def end_session(*, status: str | None = None, error: str | None = None) -> None:
    global _current
    session = _current
    try:
        if session is None or not session.enabled:
            return
        if status is None:
            if session.fields.get("verification_degraded") or getattr(session, "storage_failed", False) is True:
                status = "degraded"
            else:
                status = "ok"
        if error is None and getattr(session, "storage_failed", False) is True:
            error = "scored offers were not saved to Neon"
        _safe("close", session.close, status, error)
    finally:
        _current = None


def current() -> TierSession | None:
    return _current


def set_fields(**fields) -> None:
    if _current is not None and _current.enabled:
        _current.fields.update(fields)


def count(name: str, n: int = 1) -> None:
    if _current is not None and _current.enabled:
        _current.counters[name] = _current.counters.get(name, 0) + n


OUTCOMES = ("ok", "rate_limited", "quota_exhausted", "invalid_output", "timeout", "error")


class InvalidLLMOutput(Exception):
    """The model answered, but not in a usable shape (e.g. no forced tool call)."""


def classify_llm_error(exc: BaseException, is_quota_exhausted=None) -> str:
    if is_quota_exhausted is not None:
        try:
            if is_quota_exhausted(exc):
                return "quota_exhausted"
        except Exception:
            pass
    if isinstance(exc, (openai.RateLimitError, groq.RateLimitError)):
        return "rate_limited"
    if isinstance(exc, (openai.APITimeoutError, groq.APITimeoutError)):
        return "timeout"
    if isinstance(exc, (InvalidLLMOutput, ValidationError, json.JSONDecodeError)):
        return "invalid_output"
    return "error"


@dataclass
class LLMCall:
    stage: str
    provider: str
    request_model: str
    batch_size: int
    attempt: int
    prompt_version: str
    response_model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    outcome: str = "ok"
    error: str | None = None
    started_at: str = field(default_factory=_utcnow)

    def set_usage(self, *, response_model, input_tokens, output_tokens) -> None:
        self.response_model = response_model or None
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


def _emit_llm_call(call: LLMCall, latency_ms: int) -> None:
    session = _current
    data = {
        "id": str(uuid.uuid4()),
        "run_uuid": session.run_uuid if session is not None and session.enabled else None,
        "stage": call.stage, "provider": call.provider, "request_model": call.request_model,
        "response_model": call.response_model, "input_tokens": call.input_tokens,
        "output_tokens": call.output_tokens, "latency_ms": latency_ms,
        "batch_size": call.batch_size, "attempt": call.attempt, "outcome": call.outcome,
        "error": call.error, "prompt_version": call.prompt_version, "started_at": call.started_at,
    }
    if session is not None:
        if session.enabled:
            session._append("llm_call", data)
        return
    manual_db = os.environ.get("DATABASE_URL")
    if manual_db:
        # A manual tailoring run outside the pipeline: no session, so write through.
        flush_records([{"kind": "llm_call", "data": data}], manual_db)


@contextmanager
def llm_call(*, stage: str, provider: str, request_model: str, batch_size: int,
             attempt: int, prompt_version: str, is_quota_exhausted=None):
    call = LLMCall(stage=stage, provider=provider, request_model=request_model,
                   batch_size=batch_size, attempt=attempt, prompt_version=prompt_version)
    started = time.monotonic()
    try:
        yield call
    except BaseException as exc:
        try:
            call.outcome = classify_llm_error(exc, is_quota_exhausted)
            call.error = f"{type(exc).__name__}: {exc}"[:300]
        except Exception:
            call.outcome = "error"
        raise
    finally:
        _safe("llm call record", _emit_llm_call, call, int((time.monotonic() - started) * 1000))


def _add_query(fields: dict) -> None:
    if fields["stop_reason"] not in STOP_REASONS:
        raise ValueError(f"unknown stop_reason {fields['stop_reason']!r}")
    session = _current
    if session is None or not session.enabled:
        return
    session._append("query", {"id": str(uuid.uuid4()), "run_uuid": session.run_uuid,
                              "recorded_at": _utcnow(), **fields})


def add_query(*, role, location, work_mode, pages_walked, page_cap, cards_seen,
              offers_kept, stop_reason, cross_query_duplicates=0) -> None:
    """offers_kept: every offer the search returned. cross_query_duplicates: the
    subset that reused a description an earlier search of the same run had
    already fetched, so this search made offers_kept - cross_query_duplicates
    description requests."""
    _safe("query record", _add_query, {
        "role": role, "location": location, "work_mode": work_mode,
        "pages_walked": pages_walked, "page_cap": page_cap, "cards_seen": cards_seen,
        "offers_kept": offers_kept, "cross_query_duplicates": cross_query_duplicates,
        "stop_reason": stop_reason,
    })

