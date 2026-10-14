"""Build scored jobs in the job store for delivery tests."""
from datetime import datetime, timezone

from src.jobstore.sightings import CardInput, record_sighting

NOW = datetime(2026, 10, 14, 6, 30, tzinfo=timezone.utc)  # 08:30 in Rome (CEST)


def scored_job(conn, n, score, *, tier=1, verdict="confirmed", source="linkedin", stage="scored",
               reason="", location="Milan, Italy", when=NOW, **updates):
    job_id = record_sighting(conn, CardInput(
        source=source, source_job_id=f"{source}-{n}", source_url=f"https://www.linkedin.com/jobs/view/job-{n}",
        title=f"AI Engineer {n}", company=f"Acme {n}", location=location,
        description="We build things.", description_source=source), when).job_id
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET tier = %s, score = %s, comment = 'Good fit.', summary = %s, "
            "remote_verdict = %s, remote_reason = %s, stage = %s, stage_reason = %s "
            "WHERE id = %s",
            (tier, score, f"Summary {n}.", verdict, reason, stage,
             "remote_rejected" if stage == "filtered_out" else "", job_id))
        for column, value in updates.items():
            cur.execute(f"UPDATE jobs SET {column} = %s WHERE id = %s", (value, job_id))
    conn.commit()
    return job_id
