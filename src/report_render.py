"""Turn report_data dataclasses into plain text.

One generator serves both the morning Telegram report and scripts/run_report.py,
so what the captain reads on his phone and what is read on the server can never
disagree. Plain text only: it is sent with parse_mode=None, because model names,
error texts and stop reasons are full of Markdown metacharacters.
"""
from zoneinfo import ZoneInfo

from src.report_data import TIER_FLAGS, TIER_NAMES, TierSettings

_ROME = ZoneInfo("Europe/Rome")
_STAGE_LABELS = (("scoring", "Scoring"), ("verification", "Verification"), ("tailoring", "Tailoring"))
_LIMIT_WARN_RATIO = 0.9


def short_model(model: str) -> str:
    return model.rsplit("/", 1)[-1].removesuffix(":free")


def fmt_tokens(n) -> str:
    if n is None:
        return "-"
    return f"{round(n / 1000)}k" if n >= 1000 else str(n)


def _fmt_limit_amount(unit: str, n) -> str:
    # An unknown limit is "?", deliberately distinct from fmt_tokens' "-" for
    # "no data": one means "we don't know the ceiling", the other "nothing ran".
    if n is None:
        return "?"
    return fmt_tokens(n) if unit == "tokens" else str(n)


def _pct(used, per_day) -> str:
    return f" ({round(100 * used / per_day)}%)" if per_day else ""


def _duration(start, end) -> str:
    if start is None or end is None:
        return "?"
    minutes = int((end - start).total_seconds() // 60)
    return f"{minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def _tier_label(n: int) -> str:
    return f"Tier {n} ({TIER_NAMES.get(n, '?')})"


def collect_warnings(report, *, settings, expected_tiers=(1, 2, 3, 4)) -> list[str]:
    warnings = []
    for n in expected_tiers:
        tier = report.tiers.get(n)
        if tier is None:
            warnings.append(f"⚠ {_tier_label(n)} NOT RECORDED: no run record for this tier")
            continue
        if tier.status == "running":
            warnings.append(f"⚠ {_tier_label(n)} CRASHED: its record was never closed")
        elif tier.status == "failed":
            warnings.append(f"⚠ {_tier_label(n)} FAILED: {tier.error or 'no error text'}")
        if tier.attempts > 1:
            warnings.append(f"⚠ {_tier_label(n)} needed {tier.attempts} attempts")
        failed, total = tier.verification_batches_failed or 0, tier.verification_batches_total or 0
        if tier.verification_degraded:
            warnings.append(f"⚠ {_tier_label(n)} verification DEGRADED: {failed} of {total} batches failed")
        elif failed:
            warnings.append(f"⚠ {_tier_label(n)} verification: {failed} of {total} batches failed")
        if tier.telemetry_ok is False:
            warnings.append(f"⚠ {_tier_label(n)} telemetry incomplete: some records may be missing")
    for limit in report.limits:
        if limit.per_day and limit.used >= _LIMIT_WARN_RATIO * limit.per_day:
            warnings.append(
                f"⚠ {limit.provider} {short_model(limit.model) if limit.model != '*' else 'free models'} "
                f"limit at {round(100 * limit.used / limit.per_day)}% "
                f"({_fmt_limit_amount(limit.unit, limit.used)}/{_fmt_limit_amount(limit.unit, limit.per_day)} {limit.unit})")
    if report.previous_commit and report.git_commit and report.git_commit != report.previous_commit:
        subject = f" \"{report.commit_subject}\"" if report.commit_subject else ""
        warnings.append(f"⚠ New code live since the last run: {report.git_commit[:8]}{subject}")
    if report.git_dirty:
        warnings.append("⚠ The server's copy has uncommitted edits: numbers may not match any commit")
    if getattr(report, "limits_unconfigured", False):
        warnings.append("⚠ Provider limits could not be loaded: ceilings shown as unknown")
    return warnings


def _is_informational(warning: str) -> bool:
    """Code-identity notes. They do not mean the run itself was unhealthy."""
    return "New code live" in warning or "uncommitted edits" in warning


def _limit_header_line(limit) -> str:
    name = "free models" if limit.model == "*" else short_model(limit.model)
    used = _fmt_limit_amount(limit.unit, limit.used)
    cap = _fmt_limit_amount(limit.unit, limit.per_day)
    return f"  {limit.provider:<11}{name:<28}{used} / {cap} {limit.unit}{_pct(limit.used, limit.per_day)}"


def _summary_block(report, *, settings, expected_tiers) -> str:
    warnings = collect_warnings(report, settings=settings, expected_tiers=expected_tiers)
    ok = sum(1 for n in expected_tiers if n in report.tiers and report.tiers[n].status == "ok")
    health = [w for w in warnings if not _is_informational(w)]
    if ok == len(expected_tiers) and not health:
        verdict = "ALL OK"
    elif ok != len(expected_tiers):
        verdict = f"{ok} of {len(expected_tiers)} tiers ok"
    else:
        verdict = "CHECK"
    day = report.started_at.astimezone(_ROME).strftime("%d/%m")
    code = (report.git_commit or "unknown")[:8]
    lines = warnings + [
        f"Run health {day} · {_duration(report.started_at, report.finished_at)} · {verdict} · code {code}",
        "Limits today (whole day, all tiers):",
    ] + [_limit_header_line(l) for l in report.limits]
    lines.append(f"LLM calls: {report.llm_calls} · {report.llm_failed} failed"
                 + (f" · slowest 5% over {report.p95_latency_ms / 1000:.1f}s"
                    if report.p95_latency_ms else ""))
    return "\n".join(lines)


def _stage_line(report, tier_no: int, stage: str, label: str, verification_enabled: bool) -> str:
    if stage == "verification" and not verification_enabled:
        return f"  {label:<13} not used on this tier"
    rows = [r for r in report.stages if r.tier == tier_no and r.stage == stage]
    if not rows:
        return f"  {label:<13} no calls"
    rows.sort(key=lambda r: r.calls, reverse=True)
    models = " + ".join(f"{short_model(r.model)} ({r.calls} call{'s' if r.calls != 1 else ''})"
                        for r in rows) if len(rows) > 1 else short_model(rows[0].model)
    calls = sum(r.calls for r in rows)
    tokens = sum(r.tokens for r in rows)
    primary = rows[0]
    limit = next((l for l in report.limits if l.provider == primary.provider
                  and l.model == primary.request_model), None) or \
        next((l for l in report.limits if l.provider == primary.provider and l.model == "*"), None)
    tail = ""
    if limit is not None:
        unit = "tok" if limit.unit == "tokens" else "req"
        so_far = limit.used_through_tier.get(tier_no, limit.used)
        tail = (f" · day so far {_fmt_limit_amount(limit.unit, so_far)}/"
                f"{_fmt_limit_amount(limit.unit, limit.per_day)} {unit}")
    return f"  {label:<13} {models} · {calls} calls · {fmt_tokens(tokens)} tok{tail}"


def _tier_block(report, n: int, settings) -> str:
    head = f"{TIER_FLAGS.get(n, '')} {_tier_label(n)}"
    tier = report.tiers.get(n)
    if tier is None:
        return f"{head} · NOT RECORDED"
    status = {"running": "CRASHED"}.get(tier.status, tier.status)
    queries = [q for q in report.queries if q.tier == n]
    cap_hits = sum(1 for q in queries if q.stop_reason == "cap_hit")
    cap = queries[0].page_cap if queries else "?"
    lines = [
        f"{head} · {status} · {_duration(tier.started_at, tier.finished_at)}",
        f"  Searches: {len(queries)} · {cap_hits} hit their limit ({cap} pages)",
        f"  Offers: {tier.offers_fetched if tier.offers_fetched is not None else '?'} found → "
        f"{tier.scored} scored → {tier.high} at {settings.get(n, TierSettings()).threshold}+ → "
        f"{tier.offers_packaged if tier.offers_packaged is not None else 0} packaged",
    ]
    enabled = settings.get(n, TierSettings()).verification_enabled
    lines += [_stage_line(report, n, stage, label, enabled) for stage, label in _STAGE_LABELS]
    return "\n".join(lines)


def render_day(report, *, settings, expected_tiers=(1, 2, 3, 4)) -> list[str]:
    if report is None:
        return ["⚠ Run health: no telemetry was recorded for today's run. "
                "Check the server's cron log: the run may not have started."]
    return [_summary_block(report, settings=settings, expected_tiers=expected_tiers)] + \
        [_tier_block(report, n, settings) for n in expected_tiers]


def render_day_detail(report, *, settings) -> str:
    if report is None:
        return "No run recorded for that day."
    lines = ["\n\n".join(render_day(report, settings=settings)), "", "Searches:"]
    for q in report.queries:
        reused = getattr(q, "cross_query_duplicates", 0)
        reused_note = f" ({reused} reused from an earlier search)" if reused else ""
        lines.append(f"  T{q.tier} {q.role} / {q.location} / {q.work_mode or '-'}: "
                     f"{q.pages_walked}/{q.page_cap} pages, {q.cards_seen} cards, "
                     f"{q.offers_kept} kept{reused_note}, stopped: {q.stop_reason}")
    lines.append("Tiers:")
    entries = list(getattr(report, "attempt_log", None) or [])
    if not entries:
        entries = [
            type("T", (), {"tier": n, "attempt": t.attempt, "attempts": t.attempts,
                           "status": t.status, "started_at": t.started_at,
                           "finished_at": t.finished_at, "error": t.error})()
            for n, t in sorted(report.tiers.items())
        ]
    for t in sorted(entries, key=lambda e: (e.tier, e.attempt)):
        status = {"running": "CRASHED"}.get(t.status, t.status)
        lines.append(f"  T{t.tier} attempt {t.attempt} of {t.attempts} · {status} · "
                     f"{_duration(t.started_at, t.finished_at)}"
                     + (f" · error: {t.error}" if t.error else ""))
    return "\n".join(lines)


def _split_oversized(block: str, limit: int) -> list[str]:
    pieces, current = [], ""
    for line in block.split("\n"):
        while len(line) > limit:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            pieces.append(current)
            current = line
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces


def pack_messages(blocks: list[str], limit: int = 4096) -> list[str]:
    messages, current = [], ""
    for block in blocks:
        parts = [block] if len(block) <= limit else _split_oversized(block, limit)
        for part in parts:
            candidate = f"{current}\n\n{part}" if current else part
            if len(candidate) <= limit:
                current = candidate
            else:
                messages.append(current)
                current = part
    if current:
        messages.append(current)
    return messages


def _or_dash(v) -> str:
    return "-" if v is None else str(v)


def render_trend(metrics) -> str:
    lines = [f"date    cap-hits  verif-tok  {TierSettings().threshold}+ share  llm-fail  packaged"]
    for m in metrics:
        share = f"{100 * m.high / m.scored:.1f}%" if m.scored else "-"
        caps = f"{m.cap_hits}/{m.queries}" if m.queries is not None else "-"
        fail = f"{100 * m.llm_failed / m.llm_calls:.0f}%" if m.llm_calls else "-"
        lines.append(f"{m.day:%m-%d}   {caps:>8}  {fmt_tokens(m.verification_tokens):>9}  "
                     f"{share:>8}  {fail:>8}  {m.packaged:>8}")
    return "\n".join(lines)


def _avg(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _llm_failure_rate(metrics) -> float | None:
    calls = sum(m.llm_calls or 0 for m in metrics)
    failed = sum(m.llm_failed or 0 for m in metrics)
    return (100 * failed / calls) if calls else None


def render_compare(commit: str, before, after) -> str:
    def side(ms):
        scored = sum(m.scored for m in ms)
        return {
            "Offers found / day": _avg([m.offers_fetched for m in ms]),
            "Searches hitting limit / day": _avg([m.cap_hits for m in ms]),
            "Verification tokens / day": _avg([m.verification_tokens for m in ms]),
            f"Share scoring {TierSettings().threshold}+ (%)": (100 * sum(m.high for m in ms) / scored) if scored else None,
            "LLM failure rate (%)": _llm_failure_rate(ms),
            "Packaged / day": _avg([m.packaged for m in ms]),
        }
    b, a = side(before), side(after)
    lines = [f"compare {commit[:8]}", f"{'':32}{'before (' + str(len(before)) + ' days)':>18}"
                                      f"{'after (' + str(len(after)) + ' days)':>18}{'change':>10}"]
    for key in b:
        bv, av = b[key], a[key]
        change = f"{100 * (av - bv) / bv:+.0f}%" if bv and av is not None else "-"
        lines.append(f"{key:32}{('-' if bv is None else f'{bv:.1f}'):>18}"
                     f"{('-' if av is None else f'{av:.1f}'):>18}{change:>10}")
    if min(len(before), len(after)) < 5:
        lines.append("Indicative only: fewer than 5 days on one side. Daily numbers here swing widely.")
    return "\n".join(lines)


def render_llm(models, verdicts) -> str:
    lines = ["stage         model                          calls   ok  rate-lim  quota  bad-ans  "
             "timeout   error  median  slowest5%  avg in/out"]
    for m in models:
        calls = m["calls"] or 1
        pct = lambda k: f"{100 * (m[k] or 0) / calls:.0f}%"
        lines.append(
            f"{m['stage']:<13} {short_model(m['model']):<30} {m['calls']:>5} {pct('ok'):>4} "
            f"{pct('rate_limited'):>9} {pct('quota_exhausted'):>6} {pct('invalid_output'):>8} "
            f"{pct('timeout'):>8} {pct('error'):>6} {(m['p50'] or 0) / 1000:>6.1f}s "
            f"{(m['p95'] or 0) / 1000:>9.1f}s "
            f"{_or_dash(round(m['avg_in']) if m['avg_in'] else None)}/"
            f"{_or_dash(round(m['avg_out']) if m['avg_out'] else None)}")
    if verdicts:
        lines += ["", "Verdict mix by provider (an early signal, not a correctness measure):"]
        for v in verdicts:
            total = sum(v[k] or 0 for k in ("confirmed", "unconfirmed", "rejected")) or 1
            lines.append(f"  {v['provider']:<16} confirmed {100 * (v['confirmed'] or 0) / total:.0f}%  "
                         f"unconfirmed {100 * (v['unconfirmed'] or 0) / total:.0f}%  "
                         f"rejected {100 * (v['rejected'] or 0) / total:.0f}%")
    return "\n".join(lines)
