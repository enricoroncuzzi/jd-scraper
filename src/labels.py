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

LABELS = ("golden", "not_for_me")
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


def set_label(db_url: str | None, link: str, label: str, note: str | None = None) -> dict:
    """Idempotently label a posting. Errors propagate so the caller can report them.

    Returns {'created': bool, 'replaced': bool}: created is False when the posting
    already carried this label; replaced is True when the opposite label was removed.
    """
    if label not in LABELS:
        raise ValueError(f"unknown label '{label}'")
    key = posting_key(link)
    conn = _connect(db_url)
    try:
        with conn.cursor() as cur:
            cur.execute(OFFER_LABELS_DDL)
            cur.execute(
                "DELETE FROM offer_labels WHERE posting_key = %s AND label <> %s",
                (key, label),
            )
            replaced = cur.rowcount > 0
            cur.execute(
                """
                INSERT INTO offer_labels (posting_key, link, label, offer_id, note)
                VALUES (%s, %s, %s,
                        (SELECT id FROM offers WHERE link = %s ORDER BY id DESC LIMIT 1), %s)
                ON CONFLICT (posting_key, label) DO UPDATE SET
                    offer_id = COALESCE(EXCLUDED.offer_id, offer_labels.offer_id)
                RETURNING (xmax = 0)
                """,
                (key, link, label, link, note),
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
