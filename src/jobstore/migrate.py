"""One-time migration of the legacy Neon tables into the server job store.

Reads the old `offers`, `applications` and `offer_labels` tables (never writes
to them) and writes jobs, sightings and job_applications into the job store.
Run it first against a copy of the Neon data and with --dry-run; the report
counts must balance (MigrationReport.problems() is empty) before the real run.
"""
import hashlib
import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from src.jobstore.identity import linkedin_job_id, normalize_company, normalize_title
from src.tier_scope import resolve_country

SOURCE = "linkedin"
_ROME = ZoneInfo("Europe/Rome")

_OFFER_COLUMNS = (
    "id", "link", "title", "company", "location", "work_mode", "description",
    "description_status", "score", "comment", "summary", "tier", "run_at",
    "remote_verdict", "remote_reason", "application_channel",
)
# Rows saved before offers.run_at existed take their run's timestamp.
_OFFER_SELECT = ", ".join(
    "COALESCE(o.run_at, r.run_at)" if column == "run_at" else f"o.{column}"
    for column in _OFFER_COLUMNS
)


@dataclass
class MigrationReport:
    legacy_rows: int = 0
    distinct_postings: int = 0
    distinct_links: int = 0
    jobs_created: int = 0
    already_migrated: int = 0
    sightings_created: int = 0
    seen_count_total: int = 0
    fallback_marked_failed: int = 0
    applications_total: int = 0
    applications_matched: int = 0
    applications_minimal_jobs: int = 0
    labels_total: int = 0
    labels_matched: int = 0
    labels_unmatched: list[str] = field(default_factory=list)
    seen_hashes_loaded: int = 0
    already_migrated_rows: int = 0

    def problems(self) -> list[str]:
        """Every way the counts fail to balance. Empty means the migration is complete."""
        found = []
        migrated_rows = self.seen_count_total + self.already_migrated_rows
        if migrated_rows != self.legacy_rows:
            found.append(f"offers rows {self.legacy_rows} != migrated sighting rows {migrated_rows}")
        if self.jobs_created + self.already_migrated != self.distinct_postings:
            found.append(
                f"distinct postings {self.distinct_postings} != jobs created "
                f"{self.jobs_created} + already migrated {self.already_migrated}")
        if self.applications_matched + self.applications_minimal_jobs != self.applications_total:
            found.append(
                f"applications {self.applications_total} != matched {self.applications_matched} "
                f"+ minimal {self.applications_minimal_jobs}")
        if self.labels_unmatched:
            found.append(f"{len(self.labels_unmatched)} label(s) match no migrated job")
        return found


def legacy_key(link: str) -> str:
    """Stable id of a legacy posting: the LinkedIn job id, else a hash of the link."""
    job_id = linkedin_job_id(link)
    return job_id if job_id else "legacy:" + hashlib.md5(link.encode()).hexdigest()


def is_title_only_fallback(title: str, company: str, description: str) -> bool:
    """The scraper used "<title> at <company>" when a page was unreadable."""
    return description.strip() == f"{title} at {company}"


def map_description_status(status: str | None, description: str, is_fallback: bool) -> str:
    if is_fallback or not description.strip():
        return "failed"
    return "ok" if status in (None, "ok", "partial") else "failed"


def _read_offers(legacy_conn) -> list[dict]:
    with legacy_conn.cursor() as cur:
        cur.execute(
            f"SELECT {_OFFER_SELECT} FROM offers o LEFT JOIN runs r ON r.id = o.run_id ORDER BY o.id")
        return [dict(zip(_OFFER_COLUMNS, row)) for row in cur.fetchall()]


def _insert_job(cur, latest: dict, first_run, last_run, status: str) -> object:
    cur.execute(
        """
        INSERT INTO jobs (canonical_url, title, company, company_norm, title_norm, country,
                          location_raw, work_mode, first_seen_at, last_seen_at, description,
                          description_status, description_source, tier, remote_verdict,
                          remote_reason, score, summary, comment, application_channel, stage,
                          stage_reason, stage_changed_at, delivered_at, delivery_day)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'linkedin', %s, %s, %s, %s, %s,
                %s, %s, 'delivered', 'migrated', %s, %s, %s)
        RETURNING id
        """,
        (
            latest["link"], latest["title"] or "N/A", latest["company"] or "N/A",
            normalize_company(latest["company"] or ""), normalize_title(latest["title"] or ""),
            resolve_country(latest["location"] or ""), latest["location"] or "N/A",
            latest["work_mode"] or "", first_run, last_run, latest["description"] or "", status,
            latest["tier"], latest["remote_verdict"] or "not_checked", latest["remote_reason"] or "",
            latest["score"], latest["summary"] or "", latest["comment"] or "",
            latest["application_channel"], last_run, last_run, last_run.astimezone(_ROME).date(),
        ),
    )
    return cur.fetchone()[0]


def migrate_offers(legacy_conn, store_conn, report: MigrationReport) -> dict[str, object]:
    """One job and one sighting per distinct posting. Returns {legacy_key: job_id}."""
    rows = _read_offers(legacy_conn)
    report.legacy_rows = len(rows)
    report.distinct_links = len({row["link"] for row in rows})
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(legacy_key(row["link"]), []).append(row)
    report.distinct_postings = len(groups)

    job_ids: dict[str, object] = {}
    with store_conn.cursor() as cur:
        for key, group in groups.items():
            cur.execute("SELECT job_id FROM sightings WHERE source = %s AND source_job_id = %s",
                        (SOURCE, key))
            existing = cur.fetchone()
            if existing:
                job_ids[key] = existing[0]
                report.already_migrated += 1
                report.already_migrated_rows += len(group)
                continue
            latest = group[-1]
            runs = [row["run_at"] for row in group]
            first_run, last_run = min(runs), max(runs)
            fallback = is_title_only_fallback(
                latest["title"] or "", latest["company"] or "", latest["description"] or "")
            status = map_description_status(
                latest["description_status"], latest["description"] or "", fallback)
            if fallback:
                report.fallback_marked_failed += 1
            job_id = _insert_job(cur, latest, first_run, last_run, status)
            cur.execute(
                "INSERT INTO sightings (job_id, source, source_job_id, source_url, seen_at, "
                "last_seen_at, seen_count) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (job_id, SOURCE, key, latest["link"], first_run, last_run, len(group)),
            )
            job_ids[key] = job_id
            report.jobs_created += 1
            report.sightings_created += 1
            report.seen_count_total += len(group)
    return job_ids


def migrate_applications(legacy_conn, store_conn, job_ids: dict[str, object],
                         report: MigrationReport) -> None:
    """Re-key legacy applications to job ids. A package whose link has no
    offers row gets a minimal delivered job so it can never be packaged twice."""
    with legacy_conn.cursor() as cur:
        cur.execute("SELECT link, title, company, channel, packaged_at, dry_run FROM applications "
                    "ORDER BY id")
        applications = cur.fetchall()
    report.applications_total = len(applications)
    with store_conn.cursor() as cur:
        for link, title, company, channel, packaged_at, dry_run in applications:
            key = legacy_key(link)
            job_id = job_ids.get(key)
            if job_id is None:
                # An orphan application's minimal job is not in job_ids (that map
                # is built from offers), so a later run must find it by its
                # sighting rather than insert the sighting a second time.
                cur.execute(
                    "SELECT job_id FROM sightings WHERE source = %s AND source_job_id = %s",
                    (SOURCE, key))
                existing = cur.fetchone()
                if existing:
                    job_id = existing[0]
                    job_ids[key] = job_id
            if job_id is not None:
                report.applications_matched += 1
            else:
                latest = {
                    "link": link, "title": title, "company": company, "location": "N/A",
                    "work_mode": "", "description": "", "tier": None, "remote_verdict": None,
                    "remote_reason": "", "score": None, "summary": "", "comment": "",
                    "application_channel": channel,
                }
                job_id = _insert_job(cur, latest, packaged_at, packaged_at, "failed")
                cur.execute(
                    "INSERT INTO sightings (job_id, source, source_job_id, source_url, seen_at, "
                    "last_seen_at, seen_count) VALUES (%s, %s, %s, %s, %s, %s, 1)",
                    (job_id, SOURCE, key, link, packaged_at, packaged_at))
                job_ids[key] = job_id
                report.applications_minimal_jobs += 1
            cur.execute(
                "INSERT INTO job_applications (job_id, channel, packaged_at, dry_run) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (job_id) DO NOTHING",
                (job_id, channel, packaged_at, dry_run),
            )


def check_labels(legacy_conn, job_ids: dict[str, object], report: MigrationReport) -> None:
    """Labels stay in Neon (the Mac writes them there). This only proves every
    label's posting exists as a migrated job so job_id can be backfilled later."""
    with legacy_conn.cursor() as cur:
        cur.execute("SELECT posting_key, link FROM offer_labels")
        labels = cur.fetchall()
    report.labels_total = len(labels)
    for _posting_key, link in labels:
        if legacy_key(link) in job_ids:
            report.labels_matched += 1
        else:
            report.labels_unmatched.append(link)


def import_seen_hashes(store_conn, seen_dir: str, report: MigrationReport) -> None:
    """Load data/seen_tier{1..4}.txt (md5 of each handled link) so offers the old
    system already handled, including verification rejects that were never
    stored, are not paid for again."""
    with store_conn.cursor() as cur:
        for tier in (1, 2, 3, 4):
            path = os.path.join(seen_dir, f"seen_tier{tier}.txt")
            if not os.path.exists(path):
                continue
            with open(path) as handle:
                hashes = {line.strip() for line in handle if len(line.strip()) == 32}
            for link_hash in hashes:
                cur.execute(
                    "INSERT INTO legacy_seen_hashes (link_hash, tier) VALUES (%s, %s) "
                    "ON CONFLICT (link_hash) DO NOTHING", (link_hash, tier))
                report.seen_hashes_loaded += cur.rowcount


def run_migration(legacy_conn, store_conn, *, seen_dir: str | None = None,
                  dry_run: bool = True) -> MigrationReport:
    """Migrate everything in one transaction. dry_run rolls it back."""
    report = MigrationReport()
    try:
        job_ids = migrate_offers(legacy_conn, store_conn, report)
        migrate_applications(legacy_conn, store_conn, job_ids, report)
        check_labels(legacy_conn, job_ids, report)
        if seen_dir:
            import_seen_hashes(store_conn, seen_dir, report)
        if dry_run or report.problems():
            store_conn.rollback()
        else:
            store_conn.commit()
    except BaseException:
        store_conn.rollback()
        raise
    finally:
        legacy_conn.rollback()
    return report


def format_report(report: MigrationReport) -> str:
    lines = [
        f"offers rows read:           {report.legacy_rows}",
        f"distinct links:             {report.distinct_links}",
        f"distinct postings:          {report.distinct_postings}",
        f"jobs created:               {report.jobs_created}",
        f"already migrated:           {report.already_migrated}",
        f"sightings created:          {report.sightings_created} (rows covered: "
        f"{report.seen_count_total + report.already_migrated_rows})",
        f"title-only fallback rows:   {report.fallback_marked_failed} (marked failed)",
        f"applications:               {report.applications_total} "
        f"(matched {report.applications_matched}, minimal jobs {report.applications_minimal_jobs})",
        f"labels:                     {report.labels_total} (matched {report.labels_matched})",
        f"legacy seen hashes loaded:  {report.seen_hashes_loaded}",
    ]
    for problem in report.problems():
        lines.append(f"PROBLEM: {problem}")
    for link in report.labels_unmatched[:20]:
        lines.append(f"  unmatched label link: {link}")
    return "\n".join(lines)
