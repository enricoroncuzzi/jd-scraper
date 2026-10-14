"""Telegram text for the daily delivery. Plain strings only; sending is the caller's.

The per-tier digest message is src/telegram.py's own formatter, so it is the same
text as today. The "last 24 hours of collection" block replaces the old run
statistics, and the day summary adds service health and the language-model budget.

A day sends the four tier digest messages plus this one day summary (the captain's
decision): the four per-tier collection reports are folded into the day summary
rather than sent as separate messages.
"""
from dataclasses import replace
from datetime import datetime, timedelta

from src.delivery.daily import TierDelivery
from src.delivery.health import enabled_sources, expected_services, health_lines, source_silence_allowances
from src.delivery.jobs import to_scored_offer
from src.delivery.settings import DeliverySettings
from src.llm_limits import limit_for
from src.processor.config import ProcessorConfig, TierPolicy
from src.report_data import TIER_FLAGS, TIER_NAMES
from src.report_render import pack_messages
from src.telegram import _format_message

# The monitoring line turns amber at this share of the daily request limit (spec 6.5).
BUDGET_WARN_REQUESTS = 800


def tier_digest_message(delivery: TierDelivery, policy: TierPolicy) -> str:
    return _format_message(
        [to_scored_offer(job) for job in delivery.jobs], policy.threshold, policy.greeting,
        verification_enabled=policy.remote_check_enabled, verification_degraded=False,
        deferred_count=delivery.waiting)


def _one(cur, sql: str, params: tuple = ()):
    cur.execute(sql, params)
    return cur.fetchone()[0]


def collection_stats(conn, tier: int | None, now: datetime) -> dict:
    """The last 24 hours for one tier (or tier None = jobs not yet put in a tier)."""
    since = now - timedelta(hours=24)
    tier_sql = "j.tier = %s" if tier is not None else "j.tier IS NULL"
    tier_params = (tier,) if tier is not None else ()
    with conn.cursor() as cur:
        stats = {"found": {}, "stops": {}, "filtered": {}}
        cur.execute(f"SELECT s.source, count(*) FROM sightings s JOIN jobs j ON j.id = s.job_id "
                    f"WHERE s.seen_at >= %s AND {tier_sql} GROUP BY s.source ORDER BY 2 DESC",
                    (since, *tier_params))
        stats["found"] = dict(cur.fetchall())
        if tier is not None:
            stats["slices_total"] = _one(cur, "SELECT count(*) FROM slices WHERE active AND tier = %s", (tier,))
            cur.execute("SELECT stop_reason, count(*) FROM slices WHERE active AND tier = %s "
                        "AND last_walked_at >= %s GROUP BY stop_reason ORDER BY 2 DESC", (tier, since))
            stats["stops"] = dict(cur.fetchall())
        cur.execute(f"SELECT split_part(stage_reason, ':', 1), count(*) FROM jobs j WHERE {tier_sql} "
                    "AND stage = 'filtered_out' AND stage_changed_at >= %s GROUP BY 1 ORDER BY 2 DESC",
                    (*tier_params, since))
        stats["filtered"] = dict(cur.fetchall())
        stats["need_description"] = _one(
            cur, f"SELECT count(*) FROM jobs j WHERE {tier_sql} AND stage = 'seen'", tier_params)
        stats["need_score"] = _one(
            cur, f"SELECT count(*) FROM jobs j WHERE {tier_sql} AND stage = 'described'", tier_params)
        stats["expired"] = _one(
            cur, f"SELECT count(*) FROM jobs j WHERE {tier_sql} AND stage = 'expired' "
                 "AND stage_changed_at >= %s", (*tier_params, since))
    return stats


def _counts(values: dict) -> str:
    return ", ".join(f"{name} {count}" for name, count in values.items()) or "none"


def render_tier_report(conn, tier: int, policy: TierPolicy, delivery: TierDelivery | None,
                       now: datetime) -> str:
    stats = collection_stats(conn, tier, now)
    walked = sum(stats["stops"].values())
    delivered = len(delivery.jobs) if delivery else 0
    strong = sum(1 for job in (delivery.jobs if delivery else []) if job.score >= policy.threshold)
    return "\n".join([
        f"{TIER_FLAGS[tier]} Tier {tier} ({TIER_NAMES[tier]}): last 24 hours of collection",
        f"Slices walked: {walked}/{stats['slices_total']} ({_counts(stats['stops'])})",
        f"New jobs found: {_counts(stats['found'])}",
        f"Delivered today: {delivered} ({policy.threshold}+: {strong})",
        f"Dropped: {_counts(stats['filtered'])}; expired {stats['expired']}",
        f"Waiting: {stats['need_description']} for a description, {stats['need_score']} for a score",
    ])


def _budget_lines(conn, now: datetime) -> list[str]:
    from src.jobstore import budget
    openrouter = budget.used_today(conn, "openrouter", now)
    groq = budget.used_today(conn, "groq", now)
    or_limit, groq_limit = limit_for("openrouter", "*"), limit_for("groq", "openai/gpt-oss-20b")
    cap = or_limit.per_day if or_limit and or_limit.per_day else None
    warn = " - HIGH" if openrouter.requests >= BUDGET_WARN_REQUESTS else ""
    lines = [f"Language-model calls today (UTC): OpenRouter {openrouter.requests}"
             f"{f'/{cap}' if cap else ''} requests{warn}"]
    groq_tokens = groq.input_tokens + groq.output_tokens
    groq_cap = groq_limit.per_day if groq_limit and groq_limit.per_day else None
    lines.append(f"Groq {groq_tokens:,}{f'/{groq_cap:,}' if groq_cap else ''} tokens")
    return lines


def render_day_summary(conn, cfg: ProcessorConfig, settings: DeliverySettings,
                       deliveries: dict[int, TierDelivery], now: datetime, day) -> list[str]:
    since = now - timedelta(hours=24)
    rows = []
    with conn.cursor() as cur:
        for tier in sorted(cfg.policies):
            delivery = deliveries.get(tier)
            found = sum(collection_stats(conn, tier, now)["found"].values())
            packaged = _one(cur, "SELECT count(*) FROM job_applications a JOIN jobs j ON j.id = a.job_id "
                                 "WHERE j.tier = %s AND a.packaged_at >= %s", (tier, since))
            delivered = len(delivery.jobs) if delivery else 0
            strong = sum(1 for job in (delivery.jobs if delivery else [])
                         if job.score >= cfg.policies[tier].threshold)
            rows.append(f"{TIER_FLAGS[tier]} Tier {tier}: found {found}, delivered {delivered}, "
                        f"{cfg.policies[tier].threshold}+ {strong}, packaged {packaged}")
        unassigned = sum(collection_stats(conn, None, now)["found"].values())
        waiting = _one(cur, "SELECT count(*) FROM jobs WHERE stage IN ('seen', 'described')")
    # The four per-tier collection reports are folded in here, so a day sends the
    # four tier digests plus this one day summary.
    collection_lines = []
    for tier in sorted(cfg.policies):
        collection_lines.extend(
            render_tier_report(conn, tier, cfg.policies[tier], deliveries.get(tier), now).split("\n"))
    # One source of truth: the same enabled sources and silence allowance the watchdog uses,
    # so the Services block cannot disagree with the alerts.
    service_settings = replace(settings, expected_services=expected_services(settings, enabled_sources()))
    blocks = [
        "\n".join([f"Day summary - {day.isoformat()}"] + rows
                  + ([f"Not yet in a tier: {unassigned}"] if unassigned else [])),
        "\n".join(["Last 24 hours of collection"] + collection_lines
                  + [f"{name}: {count} new jobs" for name, count in _source_totals(conn, since).items()]
                  + [f"Jobs still waiting for processing: {waiting}"]),
        "\n".join(_budget_lines(conn, now)),
        "\n".join(["Services"] + health_lines(conn, service_settings, now, source_silence_allowances())),
    ]
    return pack_messages(blocks)


def _source_totals(conn, since: datetime) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT source, count(*) FROM sightings WHERE seen_at >= %s GROUP BY source "
                    "ORDER BY 2 DESC", (since,))
        return dict(cur.fetchall())
