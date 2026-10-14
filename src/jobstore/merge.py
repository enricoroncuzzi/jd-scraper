"""Cross-source identity (spec section 3.2, rule 2) and job merging.

Two jobs are the same posting only when ALL hold: same normalized company, same
normalized title, same known country, first seen within 30 days of each other,
no sighting source in common, and exactly one such candidate exists. Anything
less clear leaves them separate.
"""

from src.jobstore.identity import choose_canonical

WINDOW_DAYS = 30

# The job that survives a merge is the most progressed one. A `filtered_out`
# verdict outranks `described`/`seen` so a paid-for decision is never redone;
# `expired` ranks lowest so a fresh sighting with a description can revive it.
STAGE_RANK = {
    "delivered": 6, "scored": 5, "filtered_out": 4, "described": 3, "seen": 2, "expired": 1,
}

_TWIN_SQL = """
SELECT j2.id
  FROM jobs j1
  JOIN jobs j2
    ON j2.id <> j1.id
   AND j2.company_norm = j1.company_norm
   AND j2.title_norm = j1.title_norm
   AND j2.country = j1.country
   AND abs(extract(epoch FROM (j2.first_seen_at - j1.first_seen_at))) <= %s * 86400
 WHERE j1.id = %s
   AND j1.company_norm <> ''
   AND j1.title_norm <> ''
   AND NOT EXISTS (
        SELECT 1
          FROM sightings s1
          JOIN sightings s2 ON s2.source = s1.source
         WHERE s1.job_id = j1.id AND s2.job_id = j2.id)
 LIMIT 2
"""


def find_twin(conn, job_id: str) -> str | None:
    """The one existing job that is clearly the same posting, or None."""
    with conn.cursor() as cur:
        cur.execute(_TWIN_SQL, (WINDOW_DAYS, job_id))
        rows = cur.fetchall()
    return rows[0][0] if len(rows) == 1 else None


def _load(cur, job_id: str) -> dict:
    cur.execute(
        "SELECT id, stage, first_seen_at, last_seen_at, posted_at, canonical_url, description, "
        "description_status, description_source FROM jobs WHERE id = %s", (job_id,))
    keys = ("id", "stage", "first_seen_at", "last_seen_at", "posted_at", "canonical_url",
            "description", "description_status", "description_source")
    return dict(zip(keys, cur.fetchone()))


def pick_survivor(a: dict, b: dict) -> tuple[dict, dict]:
    """(keep, drop): the more progressed stage, then the earlier first_seen_at, then the
    job that has a description, then the smaller id. Never depends on argument order."""
    ordered = sorted(
        (a, b),
        key=lambda job: (-STAGE_RANK[job["stage"]], job["first_seen_at"],
                         0 if job["description_status"] == "ok" else 1, str(job["id"])))
    return ordered[0], ordered[1]


def merge_jobs(conn, a: str, b: str) -> str:
    """Fold two jobs into the more progressed one and return its id.

    The survivor takes the other's sightings, the earlier first_seen_at, the
    company page as canonical_url when either has one, and the other's
    description when its own is missing. A survivor left `expired` by a
    revived description goes back to `seen`. The caller commits.
    """
    with conn.cursor() as cur:
        keep, drop = pick_survivor(_load(cur, a), _load(cur, b))
        cur.execute("UPDATE sightings SET job_id = %s WHERE job_id = %s", (keep["id"], drop["id"]))
        canonical = choose_canonical([keep["canonical_url"], drop["canonical_url"]])
        first_seen = min(keep["first_seen_at"], drop["first_seen_at"])
        last_seen = max(keep["last_seen_at"], drop["last_seen_at"])
        posted = [t for t in (keep["posted_at"], drop["posted_at"]) if t is not None]
        cur.execute(
            "UPDATE jobs SET canonical_url = %s, first_seen_at = %s, last_seen_at = %s, "
            "posted_at = %s WHERE id = %s",
            (canonical, first_seen, last_seen, min(posted) if posted else None, keep["id"]),
        )
        if keep["description_status"] != "ok" and drop["description_status"] == "ok":
            cur.execute(
                "UPDATE jobs SET description = %s, description_status = 'ok', "
                "description_source = %s, first_refused_at = NULL WHERE id = %s",
                (drop["description"], drop["description_source"], keep["id"]),
            )
            if keep["stage"] == "expired":
                cur.execute(
                    "UPDATE jobs SET stage = 'seen', stage_reason = 'revived_by_merge', "
                    "stage_changed_at = now(), attempts = 0, next_attempt_at = now() WHERE id = %s",
                    (keep["id"],),
                )
        cur.execute("DELETE FROM jobs WHERE id = %s", (drop["id"],))
    return keep["id"]


def resolve_identity(conn, job_id: str) -> str:
    """Merge `job_id` into its clear twin if it has one. Returns the surviving
    job id, which is `job_id` itself when nothing merged. The caller commits."""
    twin = find_twin(conn, job_id)
    if twin is None:
        return job_id
    return merge_jobs(conn, job_id, twin)
