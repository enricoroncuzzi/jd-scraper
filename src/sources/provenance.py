"""Where do freehire.me's hits really come from? (spec 4.3: spot-check before production)

Some hits arrive through other aggregators (whatjobs, jobgether) rather than the
employer's own board. This report groups the last N days of freehire sightings by the
origin freehire recorded (`sightings.source_detail`) and shows how many ended up with
an employer link, so a new aggregator shows up here before it shows up in a digest.

    python -m src.sources.provenance [days]
"""
import sys

from dotenv import load_dotenv

from src.jobstore.db import connect, store_url

LOOKS_LIKE_AGGREGATOR_BELOW = 0.5


def freehire_provenance(conn, days: int = 7) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT coalesce(s.source_detail, '(unknown)') AS origin, count(*) AS hits,
                   count(*) FILTER (WHERE j.canonical_url NOT LIKE 'https://freehire.me/%%') AS employer_link,
                   count(*) FILTER (WHERE j.score IS NOT NULL) AS scored,
                   count(*) FILTER (WHERE j.score >= 8) AS strong
              FROM sightings s JOIN jobs j ON j.id = s.job_id
             WHERE s.source = 'freehire' AND s.seen_at >= now() - make_interval(days => %s)
             GROUP BY 1 ORDER BY 2 DESC
            """, (days,))
        rows = [dict(zip(("origin", "hits", "employer_link", "scored", "strong"), row))
                for row in cur.fetchall()]
    for row in rows:
        row["suspect"] = row["hits"] >= 5 and row["employer_link"] / row["hits"] < LOOKS_LIKE_AGGREGATOR_BELOW
    return rows


def italian_sample(conn, size: int = 10) -> list[tuple]:
    """Italian hits to read by hand: title, company, origin, where the link points."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT j.title, j.company, s.source_detail, j.canonical_url FROM sightings s "
            "JOIN jobs j ON j.id = s.job_id WHERE s.source = 'freehire' AND j.country = 'italy' "
            "ORDER BY s.seen_at DESC LIMIT %s", (size,))
        return cur.fetchall()


def format_report(rows: list[dict], sample: list[tuple]) -> str:
    lines = ["origin: hits, with employer link, scored, 8+"]
    for row in rows:
        flag = "   <-- looks like an aggregator, add it to exclude_sources" if row["suspect"] else ""
        lines.append(f"{row['origin']}: {row['hits']}, {row['employer_link']}, {row['scored']}, "
                     f"{row['strong']}{flag}")
    lines.append("")
    lines.append("Italian hits to read by hand:")
    lines += [f"  {title} | {company} | {origin} | {url}" for title, company, origin, url in sample]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    load_dotenv()
    days = int(argv[0]) if argv else 7
    conn = connect(store_url())
    try:
        print(format_report(freehire_provenance(conn, days), italian_sample(conn)))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
