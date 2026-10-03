"""Read run observability data out of Neon into plain dataclasses.

Everything the morning report and scripts/run_report.py show comes through
here, so the phone and the server can never disagree. Report days are
Europe/Rome calendar days; provider limit windows are UTC days.
"""
import json
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import psycopg2
import psycopg2.extras

from src.database import apply_transaction_timeouts
from src.llm_limits import Limit, load_limits

TIER_NAMES = {1: "Italy", 2: "Switzerland", 3: "EU", 4: "UK"}
TIER_FLAGS = {1: "🇮🇹", 2: "🇨🇭", 3: "🇪🇺", 4: "🇬🇧"}
NO_RESPONSE_MODEL = "no response"


@dataclass(frozen=True)
class TierSettings:
    threshold: int = 7
    verification_enabled: bool = False


@dataclass
class TierRun:
    tier: int
    run_uuid: str
    run_id: int | None
    attempt: int
    attempts: int
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    git_commit: str | None
    git_dirty: bool | None
    offers_fetched: int | None
    offers_packaged: int | None
    verification_provider: str | None
    verification_tokens: int | None
    verification_batches_failed: int | None
    verification_batches_total: int | None
    verification_degraded: bool | None
    telemetry_ok: bool | None
    scored: int = 0
    high: int = 0


@dataclass
class QueryRow:
    tier: int
    role: str
    location: str
    work_mode: str | None
    pages_walked: int
    page_cap: int
    cards_seen: int
    offers_kept: int
    stop_reason: str
    cross_query_duplicates: int = 0


@dataclass
class StageRow:
    tier: int
    stage: str
    provider: str
    request_model: str
    model: str
    calls: int
    failed: int
    tokens: int


def _merge_no_response_stage_rows(rows: list[StageRow]) -> list[StageRow]:
    """Collapse unanswered calls across request models/providers per stage."""
    merged: list[StageRow] = []
    unanswered: dict[tuple[int, str], StageRow] = {}
    for row in rows:
        if row.model != NO_RESPONSE_MODEL:
            merged.append(row)
            continue
        key = (row.tier, row.stage)
        if key not in unanswered:
            combined = StageRow(
                tier=row.tier,
                stage=row.stage,
                provider=row.provider,
                request_model=row.request_model,
                model=row.model,
                calls=0,
                failed=0,
                tokens=0,
            )
            unanswered[key] = combined
            merged.append(combined)
        combined = unanswered[key]
        combined.calls += row.calls
        combined.failed += row.failed
        combined.tokens += row.tokens
    return merged


@dataclass
class LimitUse:
    provider: str
    model: str
    unit: str
    per_day: int | None
    used: int
    used_through_tier: dict[int, int] = field(default_factory=dict)


@dataclass
class DayReport:
    daily_run_id: str
    started_at: datetime
    finished_at: datetime | None
    git_commit: str | None
    git_dirty: bool
    previous_commit: str | None
    commit_subject: str | None
    tiers: dict[int, TierRun]
    queries: list[QueryRow]
    stages: list[StageRow]
    limits: list[LimitUse]
    llm_calls: int
    llm_failed: int
    p95_latency_ms: float | None
    attempt_log: list = field(default_factory=list)
    limits_unconfigured: bool = False


@dataclass
class AttemptView:
    tier: int
    attempt: int
    attempts: int
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None


@dataclass
class DailyMetrics:
    day: date
    commits: set[str]
    offers_fetched: int | None
    cap_hits: int | None
    queries: int | None
    verification_tokens: int | None
    scored: int
    high: int
    llm_calls: int | None
    llm_failed: int | None
    packaged: int


def load_tier_settings(paths: list[str]) -> dict[int, TierSettings]:
    settings: dict[int, TierSettings] = {}
    for path in paths:
        try:
            with open(path) as f:
                data = json.load(f)
            settings[int(data.get("tier", 0))] = TierSettings(
                threshold=int((data.get("scoring") or {}).get("threshold", TierSettings().threshold)),
                verification_enabled=bool((data.get("remote_check") or {}).get("enabled", False)),
            )
        except Exception:
            continue
    return settings


def git_is_ancestor(ancestor: str, descendant: str, repo_dir: str = ".") -> bool:
    try:
        result = subprocess.run(["git", "merge-base", "--is-ancestor", ancestor, descendant],
                                cwd=repo_dir, capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def git_subject(commit: str, repo_dir: str = ".") -> str | None:
    try:
        result = subprocess.run(["git", "log", "-1", "--format=%s", commit],
                                cwd=repo_dir, capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None
    except Exception:
        return None


def split_before_after(metrics: list[DailyMetrics], commit: str, is_ancestor):
    after_days = [m.day for m in metrics if any(is_ancestor(commit, c) for c in m.commits)]
    if not after_days:
        return list(metrics), []
    split = min(after_days)
    return [m for m in metrics if m.day < split], [m for m in metrics if m.day >= split]


def _threshold_case(settings: dict[int, TierSettings]) -> str:
    whens = " ".join(f"WHEN {int(t)} THEN {int(s.threshold)}" for t, s in sorted(settings.items()))
    fallback = TierSettings().threshold
    return f"CASE tier {whens} ELSE {fallback} END" if whens else str(fallback)


def _cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def _find_daily_run_id(cur, day: date | None) -> str | None:
    if day is None:
        cur.execute("SELECT daily_run_id FROM runs WHERE daily_run_id IS NOT NULL "
                    "ORDER BY started_at DESC LIMIT 1")
    else:
        cur.execute("SELECT daily_run_id FROM runs WHERE daily_run_id IS NOT NULL AND "
                    "(started_at AT TIME ZONE 'Europe/Rome')::date = %s "
                    "ORDER BY started_at DESC LIMIT 1", (day,))
    row = cur.fetchone()
    return row["daily_run_id"] if row else None


def _limit_usage(cur, limit, day_start, day_end, until) -> int:
    if limit.unit == "requests":
        sql = "SELECT COUNT(*) AS used FROM llm_calls WHERE provider = %s"
        params = [limit.provider]
    else:
        sql = ("SELECT COALESCE(SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)), 0) "
               "AS used FROM llm_calls WHERE provider = %s")
        params = [limit.provider]
    if limit.model != "*":
        sql += " AND request_model = %s"
        params.append(limit.model)
    sql += " AND started_at >= %s AND started_at < %s"
    params += [day_start, day_end]
    if until is not None:
        sql += " AND started_at <= %s"
        params.append(until)
    cur.execute(sql, params)
    return int(cur.fetchone()["used"])


def utc_day_bounds(moment: datetime) -> tuple[datetime, datetime]:
    """The UTC calendar day that contains moment. Provider limits reset on that boundary."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    start = moment.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def catalog_for_report() -> tuple[list[Limit], bool]:
    """Limits to display. An empty catalog is a broken config, shown as unknown ceilings."""
    loaded = load_limits()
    if loaded:
        return loaded, False
    return [
        Limit("groq", "openai/gpt-oss-20b", "tokens", None),
        Limit("openrouter", "*", "requests", None),
    ], True


def _connect(db_url: str):
    conn = psycopg2.connect(db_url, connect_timeout=10)
    return apply_transaction_timeouts(conn)


def load_day(db_url: str, *, settings: dict[int, TierSettings], daily_run_id: str | None = None,
             day: date | None = None) -> DayReport | None:
    conn = _connect(db_url)
    try:
        with _cursor(conn) as cur:
            daily_run_id = daily_run_id or _find_daily_run_id(cur, day)
            if not daily_run_id:
                return None
            cur.execute("SELECT * FROM runs WHERE daily_run_id = %s ORDER BY tier, attempt",
                        (daily_run_id,))
            rows = cur.fetchall()
            if not rows:
                return None
            tiers: dict[int, TierRun] = {}
            attempt_log: list[AttemptView] = []
            for row in rows:
                tier_rows = [r for r in rows if r["tier"] == row["tier"]]
                attempts = max((r["attempt"] or 1) for r in tier_rows)
                attempt_log.append(AttemptView(
                    tier=row["tier"], attempt=row["attempt"] or 1, attempts=attempts,
                    status=row["status"] or "running", started_at=row["started_at"],
                    finished_at=row["finished_at"], error=row["error"],
                ))
                tiers[row["tier"]] = TierRun(
                    tier=row["tier"], run_uuid=str(row["run_uuid"]), run_id=row["id"],
                    attempt=row["attempt"] or 1, attempts=attempts, status=row["status"] or "running",
                    started_at=row["started_at"], finished_at=row["finished_at"], error=row["error"],
                    git_commit=row["git_commit"], git_dirty=row["git_dirty"],
                    offers_fetched=row["offers_fetched"], offers_packaged=row["offers_packaged"],
                    verification_provider=row["verification_provider"],
                    verification_tokens=row["verification_tokens"],
                    verification_batches_failed=row["verification_batches_failed"],
                    verification_batches_total=row["verification_batches_total"],
                    verification_degraded=row["verification_degraded"],
                    telemetry_ok=row["telemetry_ok"],
                )  # later attempts overwrite earlier ones: rows are ordered by attempt
            latest_uuids = [t.run_uuid for t in tiers.values()]
            for tier in tiers.values():
                threshold = settings.get(tier.tier, TierSettings()).threshold
                cur.execute("SELECT COUNT(*) AS scored, COUNT(*) FILTER (WHERE score >= %s) AS high "
                            "FROM offers WHERE run_id = %s", (threshold, tier.run_id))
                counts = cur.fetchone()
                tier.scored, tier.high = int(counts["scored"]), int(counts["high"])
            cur.execute(
                "SELECT r.tier, q.role, q.location, q.work_mode, q.pages_walked, q.page_cap, "
                "q.cards_seen, q.offers_kept, q.stop_reason, "
                "COALESCE(q.cross_query_duplicates, 0) AS cross_query_duplicates FROM run_queries q "
                "JOIN runs r ON r.run_uuid = q.run_uuid WHERE r.run_uuid = ANY(%s::uuid[]) "
                "ORDER BY r.tier, q.recorded_at", (latest_uuids,))
            queries = [QueryRow(**r) for r in cur.fetchall()]
            cur.execute(
                "SELECT r.tier, c.stage, c.provider, c.request_model, "
                f"COALESCE(c.response_model, '{NO_RESPONSE_MODEL}') AS model, COUNT(*) AS calls, "
                "COUNT(*) FILTER (WHERE c.outcome <> 'ok') AS failed, "
                "COALESCE(SUM(COALESCE(c.input_tokens, 0) + COALESCE(c.output_tokens, 0)), 0) AS tokens "
                "FROM llm_calls c JOIN runs r ON r.run_uuid = c.run_uuid "
                "WHERE r.daily_run_id = %s "
                "GROUP BY r.tier, c.stage, c.provider, c.request_model, model "
                "ORDER BY r.tier, c.stage, calls DESC", (daily_run_id,))
            stage_rows = [
                StageRow(**{**r, "calls": int(r["calls"]), "failed": int(r["failed"]),
                            "tokens": int(r["tokens"])})
                for r in cur.fetchall()
            ]
            stages = _merge_no_response_stage_rows(stage_rows)
            cur.execute(
                "SELECT COUNT(*) AS calls, COUNT(*) FILTER (WHERE c.outcome <> 'ok') AS failed, "
                "percentile_cont(0.95) WITHIN GROUP (ORDER BY c.latency_ms) AS p95 "
                "FROM llm_calls c JOIN runs r ON r.run_uuid = c.run_uuid WHERE r.daily_run_id = %s",
                (daily_run_id,))
            totals = cur.fetchone()
            started = min(r["started_at"] for r in rows if r["started_at"] is not None)
            finished_values = [r["finished_at"] for r in rows if r["finished_at"] is not None]
            # Provider limits reset on UTC midnights. The header is the UTC day
            # the run closed on. Each tier's "day so far" is that tier's own
            # UTC day, so a run that crosses midnight does not drop the later calls.
            end_moment = max(finished_values) if finished_values else started
            header_start, header_end = utc_day_bounds(end_moment)
            catalog, limits_unconfigured = catalog_for_report()
            limits = []
            for limit in catalog:
                use = LimitUse(limit.provider, limit.model, limit.unit, limit.per_day,
                               _limit_usage(cur, limit, header_start, header_end, None))
                for tier in tiers.values():
                    moment = tier.finished_at or datetime.now(timezone.utc)
                    tier_start, tier_end = utc_day_bounds(moment)
                    use.used_through_tier[tier.tier] = _limit_usage(
                        cur, limit, tier_start, tier_end, moment)
                limits.append(use)
            last = max(rows, key=lambda r: r["started_at"] or started)
            cur.execute(
                "SELECT git_commit FROM runs WHERE daily_run_id IS NOT NULL AND daily_run_id <> %s "
                "AND started_at < %s AND git_commit IS NOT NULL ORDER BY started_at DESC LIMIT 1",
                (daily_run_id, started))
            previous = cur.fetchone()
        commit = last["git_commit"]
        return DayReport(
            daily_run_id=daily_run_id, started_at=started,
            finished_at=max(finished_values) if finished_values else None,
            git_commit=commit, git_dirty=any(r["git_dirty"] for r in rows),
            previous_commit=previous["git_commit"] if previous else None,
            commit_subject=git_subject(commit) if commit and commit != "unknown" else None,
            tiers=tiers, queries=queries, stages=stages, limits=limits,
            llm_calls=int(totals["calls"]), llm_failed=int(totals["failed"]),
            p95_latency_ms=totals["p95"],
            attempt_log=attempt_log, limits_unconfigured=limits_unconfigured,
        )
    finally:
        conn.close()


def load_daily_metrics(db_url: str, *, since: date, settings: dict[int, TierSettings]) -> list[DailyMetrics]:
    conn = _connect(db_url)
    try:
        with _cursor(conn) as cur:
            cur.execute(
                "WITH latest AS (SELECT DISTINCT ON (COALESCE(daily_run_id, id::text), tier) * "
                "FROM runs WHERE (COALESCE(started_at, run_at) AT TIME ZONE 'Europe/Rome')::date >= %s "
                "ORDER BY COALESCE(daily_run_id, id::text), tier, attempt DESC NULLS LAST) "
                "SELECT (COALESCE(started_at, run_at) AT TIME ZONE 'Europe/Rome')::date AS day, "
                "SUM(offers_fetched) AS offers_fetched, SUM(verification_tokens) AS verification_tokens, "
                "array_remove(array_agg(DISTINCT git_commit), NULL) AS commits, "
                "array_agg(run_uuid) AS uuids FROM latest GROUP BY day ORDER BY day", (since,))
            runs = {r["day"]: r for r in cur.fetchall()}
            cur.execute(
                "SELECT (r.started_at AT TIME ZONE 'Europe/Rome')::date AS day, COUNT(*) AS queries, "
                "COUNT(*) FILTER (WHERE q.stop_reason = 'cap_hit') AS cap_hits FROM run_queries q "
                "JOIN runs r ON r.run_uuid = q.run_uuid WHERE (r.started_at AT TIME ZONE 'Europe/Rome')::date >= %s "
                "GROUP BY day", (since,))
            queries = {r["day"]: r for r in cur.fetchall()}
            cur.execute(
                f"SELECT (run_at AT TIME ZONE 'Europe/Rome')::date AS day, COUNT(*) AS scored, "
                f"COUNT(*) FILTER (WHERE score >= {_threshold_case(settings)}) AS high FROM offers "
                f"WHERE (run_at AT TIME ZONE 'Europe/Rome')::date >= %s GROUP BY day", (since,))
            offers = {r["day"]: r for r in cur.fetchall()}
            cur.execute(
                "SELECT (started_at AT TIME ZONE 'Europe/Rome')::date AS day, COUNT(*) AS calls, "
                "COUNT(*) FILTER (WHERE outcome <> 'ok') AS failed FROM llm_calls "
                "WHERE (started_at AT TIME ZONE 'Europe/Rome')::date >= %s GROUP BY day", (since,))
            llm = {r["day"]: r for r in cur.fetchall()}
            cur.execute(
                "SELECT (packaged_at AT TIME ZONE 'Europe/Rome')::date AS day, COUNT(*) AS packaged "
                "FROM applications WHERE (packaged_at AT TIME ZONE 'Europe/Rome')::date >= %s "
                "GROUP BY day", (since,))
            packaged = {r["day"]: r for r in cur.fetchall()}
        days = sorted(set(runs) | set(offers) | set(packaged))
        return [DailyMetrics(
            day=d,
            commits=set(runs[d]["commits"]) if d in runs else set(),
            offers_fetched=runs[d]["offers_fetched"] if d in runs else None,
            cap_hits=int(queries[d]["cap_hits"]) if d in queries else None,
            queries=int(queries[d]["queries"]) if d in queries else None,
            verification_tokens=runs[d]["verification_tokens"] if d in runs else None,
            scored=int(offers[d]["scored"]) if d in offers else 0,
            high=int(offers[d]["high"]) if d in offers else 0,
            llm_calls=int(llm[d]["calls"]) if d in llm else None,
            llm_failed=int(llm[d]["failed"]) if d in llm else None,
            packaged=int(packaged[d]["packaged"]) if d in packaged else 0,
        ) for d in days]
    finally:
        conn.close()


def load_llm_view(db_url: str, *, since: date, stage: str | None = None):
    conn = _connect(db_url)
    try:
        with _cursor(conn) as cur:
            sql = (
                f"SELECT stage, provider, COALESCE(response_model, '{NO_RESPONSE_MODEL}') AS model, "
                "COUNT(*) AS calls, "
                + ", ".join(f"COUNT(*) FILTER (WHERE outcome = '{o}') AS {o}" for o in
                            ("ok", "rate_limited", "quota_exhausted", "invalid_output", "timeout", "error"))
                + ", percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50, "
                "percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95, "
                "AVG(input_tokens) AS avg_in, AVG(output_tokens) AS avg_out FROM llm_calls "
                "WHERE (started_at AT TIME ZONE 'Europe/Rome')::date >= %s"
            )
            params: list = [since]
            if stage:
                sql += " AND stage = %s"
                params.append(stage)
            sql += " GROUP BY stage, provider, model ORDER BY stage, calls DESC"
            cur.execute(sql, params)
            models = [dict(r) for r in cur.fetchall()]
            cur.execute(
                "SELECT verification_provider AS provider, SUM(verification_confirmed) AS confirmed, "
                "SUM(verification_unconfirmed) AS unconfirmed, SUM(verification_rejected) AS rejected "
                "FROM runs WHERE verification_provider IS NOT NULL AND "
                "(started_at AT TIME ZONE 'Europe/Rome')::date >= %s GROUP BY verification_provider",
                (since,))
            verdicts = [dict(r) for r in cur.fetchall()]
        return models, verdicts
    finally:
        conn.close()
