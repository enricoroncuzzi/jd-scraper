"""Auto-tailoring above the cutoff, as today (src/autoapply/pipeline.py).

Draft-and-notify only: nothing here submits an application. For every
above-threshold job that is confirmed (or tier 2's not_checked), has a note on disk
and has not been packaged: classify the application channel, tailor the CV, cover
letter and recruiter message with the existing tailor.run, write the package
manifest, and record the job in job_applications.

`advised` (remote, eligibility to check) and `not_stated` jobs are deliberately
not packaged: the captain decides those by hand, and packaging one would spend a
package on a job that is not confirmed remote without the advice in front of him.
The filter lives in the SQL below (NOT 'unconfirmed': the legacy value reads as
not stated). In dry-run the Telegram "package
ready" notice is skipped but the row is still written, so the same job is never
tailored twice.

The classify request is a plain GET of the job page. For a LinkedIn page it goes
through the shared LinkedIn pace like every other LinkedIn request; when the
schedule gate is closed there, the candidate is deferred to the next pass rather
than treated as a failure.
"""
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from src.autoapply.classify import classify_channel
from src.autoapply.package import notify_package, write_manifest
from src.delivery.daily import note_path
from src.delivery.jobs import DeliveryJob, load_jobs, to_scored_offer
from src.delivery.settings import DeliverySettings
from src.linkedin.pace import PaceBusy, PaceGateClosed
from src.processor.config import ProcessorConfig
from src.scorer import _openrouter_quota_exhausted
from src.services.pulse import pulse


@dataclass(frozen=True)
class PackagingConfig:
    cv_master_path: str
    css_path: str
    llm_api_key: str


# A failed tailoring is retried 1h, 6h, 24h, then left alone; candidates are also
# bounded by age so an old delivered job is not re-tailored forever.
PACKAGE_MAX_AGE_DAYS = 3
PACKAGE_RETRY_DELAYS = (timedelta(hours=1), timedelta(hours=6), timedelta(hours=24))
PACKAGE_MAX_ATTEMPTS = 3


def check_cv_sources(pkg: PackagingConfig) -> None:
    """Fail up front, loudly, when the CV source is not deployed (as run_autoapply does)."""
    if not os.path.exists(pkg.cv_master_path):
        raise FileNotFoundError(
            f"CV master source not found at {pkg.cv_master_path!r} - set CV_MASTER_PATH")
    if pkg.css_path and not os.path.exists(pkg.css_path):
        raise FileNotFoundError(f"CV CSS source not found at {pkg.css_path!r} - set CV_CSS_PATH")


def _package_failures(cur) -> dict:
    cur.execute("SELECT key, value FROM kv_state WHERE key LIKE 'package_fail:%%'")
    return {key[len("package_fail:"):]: value for key, value in cur.fetchall()}


def _gated(fail: dict | None, now: datetime) -> bool:
    if not fail:
        return False
    if fail.get("stopped"):
        return True
    next_try = fail.get("next_try_at")
    return bool(next_try) and datetime.fromisoformat(next_try) > now


def _record_package_failure(conn, job, now: datetime) -> None:
    """Remember a failed tailoring so the job is not retried every pass: 1h, 6h,
    24h, then stop. The halt review file stays as the visible result."""
    key = f"package_fail:{job.id}"
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM kv_state WHERE key = %s", (key,))
        row = cur.fetchone()
        attempts = (row[0].get("attempts", 0) if row else 0) + 1
        if attempts >= PACKAGE_MAX_ATTEMPTS:
            value = {"attempts": attempts, "stopped": True}
        else:
            value = {"attempts": attempts, "stopped": False,
                     "next_try_at": (now + PACKAGE_RETRY_DELAYS[attempts - 1]).isoformat()}
        cur.execute("INSERT INTO kv_state (key, value, updated_at) VALUES (%s, %s::jsonb, %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at",
                    (key, json.dumps(value), now))
    conn.commit()


def candidates(conn, cfg: ProcessorConfig, now: datetime) -> list[DeliveryJob]:
    enabled = [(tier, p.threshold) for tier, p in sorted(cfg.policies.items()) if p.autoapply_enabled]
    if not enabled:
        return []
    conditions = " OR ".join("(j.tier = %s AND j.score >= %s)" for _ in enabled)
    params = tuple(value for pair in enabled for value in pair)
    # Only a confirmed (or unchecked) remote job is packaged; see the module docstring.
    jobs = load_jobs(
        conn,
        f"j.stage IN ('scored', 'delivered') AND j.note_day IS NOT NULL "
        f"AND j.note_day >= %s "
        f"AND j.remote_verdict IN ('confirmed', 'not_checked') AND ({conditions}) "
        "AND NOT EXISTS (SELECT 1 FROM job_applications a WHERE a.job_id = j.id)",
        (now.date() - timedelta(days=PACKAGE_MAX_AGE_DAYS), *params),
        order="j.score DESC, j.first_seen_at")
    with conn.cursor() as cur:
        failures = _package_failures(cur)
    return [job for job in jobs if not _gated(failures.get(job.id), now)]


def is_linkedin_link(link: str) -> bool:
    """True when the application link is hosted on linkedin.com."""
    return urlsplit(link).netloc.lower().endswith("linkedin.com")


def classify_through_pace(pace, link: str, classify=classify_channel) -> str:
    if is_linkedin_link(link):
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
    due = candidates(conn, cfg, now)
    if not due:
        return []
    check_cv_sources(pkg)
    results = []
    linkedin_waits = False
    for job in due:
        pulse({"packaging": len(results)})
        policy = cfg.policies[job.tier]
        if linkedin_waits and is_linkedin_link(job.canonical_url):
            # The pace already said no to this pass: do not spend its turn budget again.
            print(f"[deliver] LinkedIn is busy or closed, deferring packaging of {job.company}")
            continue
        try:
            channel = classify_through_pace(pace, job.canonical_url, classify)
        except (PaceGateClosed, PaceBusy):
            # LinkedIn is closed (quiet hours or the shared daily cap) or had no turn in
            # time. Leave the job a candidate for the next pass: no failure row, no
            # channel, no notice. Other hosts still package in this pass.
            print(f"[deliver] LinkedIn is busy or closed, deferring packaging of {job.company}")
            linkedin_waits = True
            continue
        path = note_path(settings, job, job.note_day)
        try:
            directory = tailor_run(path, settings.output_path, pkg.cv_master_path, pkg.css_path,
                                   pkg.llm_api_key)
        except Exception as exc:  # noqa: BLE001 - one bad job must not stop the rest
            print(f"[deliver] tailoring failed for {job.company}: {exc}")
            if _openrouter_quota_exhausted(exc):
                print("[deliver] OpenRouter daily quota exhausted, stopping packaging")
                break
            _record_package_failure(conn, job, now)
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
