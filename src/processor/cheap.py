"""Stage 1: the no-network checks (spec section 5, step 1).

For a `seen` job with no tier yet: skip what the old system already handled,
merge a clear cross-source twin, put the job in a tier or drop it as out of
scope, optionally apply the title pre-filter, and send a job that already has a
description straight to the language check. A `seen` job that already has a
tier and a description is checked again too: a merge can adopt the description
from a twin after the job was tiered, leaving it `seen` with nothing else
to advance it.
"""
import hashlib
from datetime import datetime

from src.jobstore.merge import resolve_identity
from src.processor.config import ProcessorConfig
from src.processor.language import classify_language
from src.processor.state import JobRow, load_job, set_stage
from src.processor.titles import title_hits_exclude
from src.tier_scope import TIER3_ALLOWED_COUNTRIES

TIER_BY_COUNTRY: dict[str, int] = {
    "italy": 1, "switzerland": 2, "san marino": 2, "united kingdom": 4,
    **{country: 3 for country in TIER3_ALLOWED_COUNTRIES},
}


def provisional_tier(country: str | None, tier_hint: int | None) -> int | None:
    """The tier for a resolved country, the source's hint when the location names
    no country ("Europe", "Remote"; the remote check decides those), or None when
    the country is known but belongs to no tier."""
    if country is None:
        return tier_hint
    return TIER_BY_COUNTRY.get(country)


def _handled_before_cutover(cur, job_id) -> bool:
    cur.execute(
        "SELECT 1 FROM sightings s JOIN legacy_seen_hashes l ON l.link_hash = md5(s.source_url) "
        "WHERE s.job_id = %s LIMIT 1", (job_id,))
    return cur.fetchone() is not None


def finish_with_description(cur, job: JobRow, now: datetime) -> None:
    """The job has a usable description: run the language check and move on."""
    language, passes = classify_language(job.description)
    cur.execute("UPDATE jobs SET language = %s WHERE id = %s", (language, job.id))
    if passes:
        set_stage(cur, job.id, "described", "", now)
    else:
        set_stage(cur, job.id, "filtered_out", f"language:{language}", now)


def check_one(conn, job_id, cfg: ProcessorConfig, now: datetime) -> str:
    """Run the cheap checks on one job. Returns what happened:
    'gone' (merged away), 'filtered', or 'tiered'."""
    with conn.cursor() as cur:
        if _handled_before_cutover(cur, job_id):
            set_stage(cur, job_id, "filtered_out", "seen_before_cutover", now)
            return "filtered"
    survivor = resolve_identity(conn, job_id)
    if survivor != job_id:
        return "gone"
    with conn.cursor() as cur:
        job = load_job(cur, job_id)
        tier = provisional_tier(job.country, job.tier_hint)
        if tier is None:
            set_stage(cur, job_id, "filtered_out", "out_of_scope", now)
            return "filtered"
        policy = cfg.policies[tier]
        if policy.title_prefilter:
            keyword = title_hits_exclude(job.title, policy.exclude_keywords)
            if keyword:
                set_stage(cur, job_id, "filtered_out", f"title_prefilter:{keyword}", now)
                return "filtered"
        cur.execute("UPDATE jobs SET tier = %s, description_class = %s WHERE id = %s",
                    (tier, cfg.fetch_order.classify(tier, job.title, job.location_raw), job_id))
        job.tier = tier
        if job.description_status == "ok" and job.description.strip():
            finish_with_description(cur, job, now)
    return "tiered"


def run_cheap_checks(conn, cfg: ProcessorConfig, now: datetime) -> int:
    """Check up to cfg.cheap_checks_per_tick seen jobs that still need it, oldest
    first: untiered jobs and tiered jobs whose description arrived later (a merged
    twin adopted it), so a present description always advances the job. Each job
    commits on its own. Returns how many were handled."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM jobs WHERE stage = 'seen' "
            "AND (tier IS NULL OR (description_status = 'ok' AND btrim(description) <> '')) "
            "ORDER BY first_seen_at LIMIT %s", (cfg.cheap_checks_per_tick,))
        job_ids = [row[0] for row in cur.fetchall()]
    handled = 0
    for job_id in job_ids:
        with conn.cursor() as cur:
            if load_job(cur, job_id) is None:  # merged away by an earlier job in this batch
                continue
        check_one(conn, job_id, cfg, now)
        conn.commit()
        handled += 1
    return handled


def legacy_hash(url: str) -> str:
    """How the old dedup log hashed a link (src/dedup.py)."""
    return hashlib.md5(url.encode()).hexdigest()
