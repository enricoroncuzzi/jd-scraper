"""Auto-tailoring above the cutoff, as today (src/autoapply/pipeline.py).

Draft-and-notify only: nothing here submits an application. For every
above-threshold job that is confirmed (or tier 2's not_checked), has a note on disk
and has not been packaged: classify the application channel, tailor the CV, cover
letter and recruiter message with the existing tailor.run, write the package
manifest, and record the job in job_applications. In dry-run the Telegram "package
ready" notice is skipped but the row is still written, so the same job is never
tailored twice.

The classify request is a plain GET of the job page. For a LinkedIn page it goes
through the shared LinkedIn pace like every other LinkedIn request.
"""
import os
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from src.autoapply.classify import classify_channel
from src.autoapply.package import notify_package, write_manifest
from src.delivery.daily import note_path
from src.delivery.jobs import DeliveryJob, load_jobs, to_scored_offer
from src.delivery.settings import DeliverySettings
from src.processor.config import ProcessorConfig
from src.scorer import _openrouter_quota_exhausted


@dataclass(frozen=True)
class PackagingConfig:
    cv_master_path: str
    css_path: str
    llm_api_key: str


def check_cv_sources(pkg: PackagingConfig) -> None:
    """Fail up front, loudly, when the CV source is not deployed (as run_autoapply does)."""
    if not os.path.exists(pkg.cv_master_path):
        raise FileNotFoundError(
            f"CV master source not found at {pkg.cv_master_path!r} - set CV_MASTER_PATH")
    if pkg.css_path and not os.path.exists(pkg.css_path):
        raise FileNotFoundError(f"CV CSS source not found at {pkg.css_path!r} - set CV_CSS_PATH")


def candidates(conn, cfg: ProcessorConfig) -> list[DeliveryJob]:
    enabled = [(tier, p.threshold) for tier, p in sorted(cfg.policies.items()) if p.autoapply_enabled]
    if not enabled:
        return []
    conditions = " OR ".join("(j.tier = %s AND j.score >= %s)" for _ in enabled)
    params = tuple(value for pair in enabled for value in pair)
    return load_jobs(
        conn,
        f"j.stage IN ('scored', 'delivered') AND j.note_day IS NOT NULL "
        f"AND j.remote_verdict IN ('confirmed', 'not_checked') AND ({conditions}) "
        "AND NOT EXISTS (SELECT 1 FROM job_applications a WHERE a.job_id = j.id)",
        params, order="j.score DESC, j.first_seen_at")


def classify_through_pace(pace, link: str, classify=classify_channel) -> str:
    if urlsplit(link).netloc.lower().endswith("linkedin.com"):
        with pace.slot("description") as slot:
            channel = classify(link)
            slot.clean() if channel != "unknown" else slot.neutral()
            return channel
    return classify(link)


def run_packaging(conn, cfg: ProcessorConfig, settings: DeliverySettings, pkg: PackagingConfig,
                  pace, *, now: datetime, tailor_run, classify=classify_channel,
                  notify=notify_package) -> list[dict]:
    """Package every due candidate. `tailor_run(note_path, output_root, cv_master,
    css, api_key)` is tailor.run. An OpenRouter daily-quota error ends the loop;
    any other failure skips that job (it is retried on the next pass)."""
    due = candidates(conn, cfg)
    if not due:
        return []
    check_cv_sources(pkg)
    results = []
    for job in due:
        policy = cfg.policies[job.tier]
        channel = classify_through_pace(pace, job.canonical_url, classify)
        path = note_path(settings, job, job.note_day)
        try:
            directory = tailor_run(path, settings.output_path, pkg.cv_master_path, pkg.css_path,
                                   pkg.llm_api_key)
        except Exception as exc:  # noqa: BLE001 - one bad job must not stop the rest
            print(f"[deliver] tailoring failed for {job.company}: {exc}")
            if _openrouter_quota_exhausted(exc):
                print("[deliver] OpenRouter daily quota exhausted, stopping packaging")
                break
            continue
        offer = to_scored_offer(job)
        write_manifest(directory, offer, channel, policy.autoapply_dry_run)
        with conn.cursor() as cur:
            cur.execute("UPDATE jobs SET application_channel = %s, archived_at = NULL WHERE id = %s",
                        (channel, job.id))
            cur.execute(
                "INSERT INTO job_applications (job_id, channel, packaged_at, dry_run) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (job_id) DO NOTHING",
                (job.id, channel, now, policy.autoapply_dry_run))
        conn.commit()
        if not policy.autoapply_dry_run:
            notify(offer, channel, directory, settings.telegram_token, settings.telegram_chat_id)
        results.append({"job_id": job.id, "channel": channel, "directory": directory,
                        "dry_run": policy.autoapply_dry_run})
    return results
