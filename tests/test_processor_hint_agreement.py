from datetime import datetime, timezone

from src.jobstore.sightings import CardInput, record_sighting
from src.processor.hint_agreement import format_report, hint_agreement

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def _job(conn, n, hint, verdict, *, tier=1, country="italy"):
    job_id = record_sighting(conn, CardInput(
        source="freehire", source_job_id=str(n), source_url=f"https://freehire.me/{n}",
        title=f"Role {n}", company=f"Co {n}", country=country, remote_hint=hint), NOW).job_id
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET tier = %s, remote_verdict = %s WHERE id = %s", (tier, verdict, job_id))


def test_agreement_is_measured_per_shortcut_and_unconfirmed_is_not_a_disagreement(store_conn):
    for n in range(9):
        _job(store_conn, n, "remote", "confirmed")
    _job(store_conn, 10, "remote", "rejected")
    _job(store_conn, 11, "remote", "unconfirmed")
    _job(store_conn, 12, "remote", "confirmed", tier=3, country="germany")  # not covered by the shortcut
    _job(store_conn, 20, "onsite", "rejected", tier=3, country="germany")
    first, second, _third = hint_agreement(store_conn)
    assert (first["agreed"], first["disagreed"], first["unconfirmed"], first["decided"]) == (9, 1, 1, 10)
    assert first["rate"] == 0.9 and first["ready"] is False  # too few decided and under 95 percent
    assert (second["agreed"], second["decided"]) == (1, 1) and second["ready"] is False


def test_a_shortcut_is_ready_only_with_enough_decided_jobs_above_95_percent(store_conn):
    for n in range(100):
        _job(store_conn, n, "remote", "confirmed")
    ready, *_ = hint_agreement(store_conn)
    assert ready["ready"] is True
    for n in range(500, 506):
        _job(store_conn, n, "remote", "rejected")
    again, *_ = hint_agreement(store_conn)
    assert again["rate"] == 100 / 106 and again["ready"] is False


def test_the_report_names_each_shortcut(store_conn):
    text = format_report(hint_agreement(store_conn))
    assert "remote + located in Italy" in text and "n/a" in text
