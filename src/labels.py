"""Golden / not-for-me labels on scraped postings (table: offer_labels).

Why a separate table rather than a boolean on offers: the same posting is stored
once per re-score, so a flag on one offers row would be lost or duplicated. A label
is keyed by posting_key (see below) and survives re-scores.

A posting holds at most one label: marking it golden removes its not_for_me label
and vice versa, because the two verdicts contradict each other.
"""
import os
import re
import urllib.parse

from src.storage import OFFER_LABELS_DDL, psycopg2
from src.tailor.jd_source import _frontmatter, parse_note
from src.writer import _slugify

LABELS = ("golden", "not_for_me")
DISPLAY = {"golden": "golden", "not_for_me": "not-for-me"}
ICON = {"golden": "⭐", "not_for_me": "👎"}
SCHEME = "jdlabel:"


def posting_key(link: str) -> str:
    """Stable identity of a posting.

    LinkedIn job URLs end in a numeric job id ('.../jobs/view/ai-engineer-at-acme-3912345678');
    the slug and host (it.linkedin.com vs www) vary between sightings, the id does not,
    so the id is the key. Any other URL falls back to host + path without query,
    fragment or trailing slash.
    """
    parsed = urllib.parse.urlsplit(link.strip())
    last = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    m = re.search(r"(\d{5,})$", last)
    if m and "linkedin." in parsed.netloc.lower():
        return f"linkedin:{m.group(1)}"
    return f"{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


_NOTE_NUMBER = re.compile(r"_(\d+)\.md$")


def note_number(note: str | None) -> int | None:
    """The job note number a note filename ends with ('acme_ai_engineer_50965.md' -> 50965).

    The deliverer writes the filename from jobs.note_no (src/writer.py::_note_filename is
    fed the delivery job's note_no), so it identifies the posting across databases even
    when the sighting that links the two never reached the archive.
    """
    match = _NOTE_NUMBER.search(note or "")
    return int(match.group(1)) if match else None


def parse_label_uri(uri: str) -> tuple[str, str]:
    """'jdlabel:golden/2026-10-11/tier1/scraped/x_1.md' -> ('golden', '2026-10-11/tier1/scraped/x_1.md')."""
    decoded = urllib.parse.unquote(uri)
    rest = decoded.split(SCHEME, 1)[1] if SCHEME in decoded else decoded
    rest = rest.lstrip("/")
    label, _, note_rel = rest.partition("/")
    if label not in LABELS:
        raise ValueError(f"unknown label '{label}', expected one of {', '.join(LABELS)}")
    if not note_rel:
        raise ValueError("label link has no note path")
    return label, note_rel


def resolve_note(note_rel: str, jd_output_root: str) -> str:
    root = os.path.realpath(jd_output_root)
    resolved = os.path.realpath(os.path.join(root, note_rel.lstrip("/")))
    if resolved != root and not resolved.startswith(root + os.sep):
        raise ValueError(f"refusing note path outside jd-output root: {note_rel}")
    return resolved


def read_note(note_path: str) -> dict:
    if not os.path.isfile(note_path):
        raise ValueError(f"note not found: {note_path}")
    jd = parse_note(note_path)
    if not jd.link:
        raise ValueError(f"note has no link: {note_path}")
    with open(note_path) as f:
        score = _frontmatter(f.read()).get("score", "")
    return {"link": jd.link, "title": jd.title, "company": jd.company, "score": score,
            "note": os.path.basename(note_path)}


def _connect(db_url: str | None):
    if not db_url:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg2.connect(db_url)


def _has_table(cur, name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (name,))
    return bool(cur.fetchone()[0])


def _needed_key(key: str) -> str:
    """The part of a posting key distinctive enough to narrow the SQL candidate scan."""
    if key.startswith("linkedin:"):
        return key.split(":", 1)[1]
    return key.split("/", 1)[1] if "/" in key else key


def _matching_job_id(cur, key: str) -> str | None:
    """A job whose canonical_url, own link, or own sighting carries this posting key."""
    if _has_table(cur, "sightings"):
        if key.startswith("linkedin:"):
            # The indexed lookup the sightings unique constraint serves: a label on a
            # LinkedIn URL usually resolves in one row, whatever the canonical URL became.
            cur.execute(
                "SELECT job_id FROM sightings WHERE source = 'linkedin' AND source_job_id = %s "
                "ORDER BY last_seen_at DESC LIMIT 1", (key.split(":", 1)[1],))
            row = cur.fetchone()
            if row:
                return row[0]
        cur.execute("SELECT job_id, source, source_job_id, source_url FROM sightings "
                    "WHERE source_url LIKE %s", (f"%{_needed_key(key)}%",))
        for job_id, _, _, source_url in cur.fetchall():
            if posting_key(source_url) == key:
                return job_id
    cur.execute("SELECT id, canonical_url FROM jobs WHERE canonical_url LIKE %s",
                (f"%{_needed_key(key)}%",))
    for job_id, url in cur.fetchall():
        if posting_key(url) == key:
            return job_id
    return None


def _job_id_for(cur, link: str, note: str | None, *, legacy_offer: bool = False) -> str | None:
    """The archived job a label is about, or None.

    The label link and the job's canonical_url can differ: the sighting that won the
    merge decides canonical_url, so a golden label on a LinkedIn URL can point at an
    Ashby job and no offers row exists for it. Match on identity, strongest first: the
    posting key in the job's sightings and canonical_url, then the note number in the
    note filename (the only link left when the matching sighting was recorded after
    the job was archived to Neon and so never arrived there).

    The note number is a last resort, not identity: a legacy note also ends in a small
    offer id (src/writer.py::_note_filename), so the filename itself must name the
    candidate job's company and title before the link is made, and a label the legacy
    offers table already identified is never second-guessed by it.
    """
    if not _has_table(cur, "jobs"):
        return None
    job_id = _matching_job_id(cur, posting_key(link))
    if job_id is not None:
        return job_id
    number = note_number(note)
    if number is None or legacy_offer:
        return None
    cur.execute("SELECT id, company, title FROM jobs WHERE note_no = %s", (number,))
    row = cur.fetchone()
    if row and note.startswith(f"{_slugify(row[1])}_{_slugify(row[2])}_"):
        return row[0]
    return None


def fill_missing_label_job_ids(cur) -> int:
    """Give every label without a job_id the archived job it is about.

    Idempotent, and a label that already has a job_id is never touched. Takes an open
    cursor so both label.py and the Neon archive copy can call it; the caller commits.
    """
    if not _has_table(cur, "jobs"):
        return 0
    cur.execute("SELECT id, link, note, offer_id FROM offer_labels WHERE job_id IS NULL")
    filled = 0
    for label_id, link, note, offer_id in cur.fetchall():
        job_id = _job_id_for(cur, link, note, legacy_offer=offer_id is not None)
        if job_id is None:
            continue
        cur.execute("UPDATE offer_labels SET job_id = %s WHERE id = %s AND job_id IS NULL",
                    (job_id, label_id))
        filled += cur.rowcount
    return filled


def set_label(db_url: str | None, link: str, label: str, note: str | None = None) -> dict:
    """Idempotently label a posting. Errors propagate so the caller can report them.

    job_id is filled with the archived job the posting is (see _job_id_for); an existing
    job_id is kept even when a match would say otherwise.
    Returns {'created': bool, 'replaced': str | None}: created is False when the posting
    already carried this label; replaced names the opposite label that was removed, if any.
    """
    if label not in LABELS:
        raise ValueError(f"unknown label '{label}'")
    key = posting_key(link)
    conn = _connect(db_url)
    try:
        with conn.cursor() as cur:
            cur.execute(OFFER_LABELS_DDL)
            cur.execute("ALTER TABLE offer_labels ADD COLUMN IF NOT EXISTS job_id UUID")
            cur.execute("SELECT id FROM offers WHERE link = %s ORDER BY id DESC LIMIT 1", (link,))
            row = cur.fetchone()
            offer_id = row[0] if row else None
            job_id = _job_id_for(cur, link, note, legacy_offer=offer_id is not None)
            cur.execute(
                "DELETE FROM offer_labels WHERE posting_key = %s AND label <> %s",
                (key, label),
            )
            replaced = next(l for l in LABELS if l != label) if cur.rowcount > 0 else None
            cur.execute(
                """
                INSERT INTO offer_labels (posting_key, link, label, offer_id, job_id, note)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (posting_key, label) DO UPDATE SET
                    offer_id = COALESCE(EXCLUDED.offer_id, offer_labels.offer_id),
                    job_id = COALESCE(offer_labels.job_id, EXCLUDED.job_id)
                RETURNING (xmax = 0)
                """,
                (key, link, label, offer_id, job_id, note),
            )
            created = bool(cur.fetchone()[0])
        conn.commit()
    finally:
        conn.close()
    return {"created": created, "replaced": replaced}


def remove_label(db_url: str | None, link: str, label: str) -> int:
    if label not in LABELS:
        raise ValueError(f"unknown label '{label}'")
    conn = _connect(db_url)
    try:
        with conn.cursor() as cur:
            cur.execute(OFFER_LABELS_DDL)
            cur.execute("ALTER TABLE offer_labels ADD COLUMN IF NOT EXISTS job_id UUID")
            cur.execute(
                "DELETE FROM offer_labels WHERE posting_key = %s AND label = %s",
                (posting_key(link), label),
            )
            removed = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return removed


def list_labels(db_url: str | None, label: str) -> list[dict]:
    if label not in LABELS:
        raise ValueError(f"unknown label '{label}'")
    conn = _connect(db_url)
    try:
        with conn.cursor() as cur:
            cur.execute(OFFER_LABELS_DDL)
            cur.execute("ALTER TABLE offer_labels ADD COLUMN IF NOT EXISTS job_id UUID")
            has_archive = _has_table(cur, "jobs")
            if has_archive:
                # Self-heal labels recorded before their job could be linked, the same
                # way the ALTER above repairs a pre-job_id table on every call.
                fill_missing_label_job_ids(cur)
                cur.execute(
                    """
                    SELECT COALESCE(o.title, j.title), COALESCE(o.company, j.company),
                           COALESCE(o.score, j.score), l.labeled_at, l.link
                    FROM offer_labels l
                    LEFT JOIN offers o ON o.id = l.offer_id
                    LEFT JOIN jobs j ON j.id = l.job_id
                    WHERE l.label = %s
                    ORDER BY l.labeled_at DESC
                    """,
                    (label,),
                )
            else:
                # The archive tables may not exist yet (archive --init has not run):
                # label list must keep working against the legacy offers table alone.
                cur.execute(
                    """
                    SELECT o.title, o.company, o.score, l.labeled_at, l.link
                    FROM offer_labels l
                    LEFT JOIN offers o ON o.id = l.offer_id
                    WHERE l.label = %s
                    ORDER BY l.labeled_at DESC
                    """,
                    (label,),
                )
            rows = cur.fetchall()
        conn.commit()
    finally:
        conn.close()
    return [
        {"title": r[0], "company": r[1], "score": r[2], "labeled_at": r[3], "link": r[4]}
        for r in rows
    ]
