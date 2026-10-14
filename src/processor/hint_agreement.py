"""Does a source's own work-mode field agree with the model's remote verdict?

The structured shortcut (processor.json `structured_remote.enabled`) skips the
model, so it ships off. While it is off, every job still gets a model verdict, and
this report compares the two on real traffic. Adopt a shortcut only when its
decided agreement is above 95 percent on at least MIN_DECIDED jobs.

    python -m src.processor.hint_agreement
"""
import sys

from dotenv import load_dotenv

from src.jobstore.db import connect, store_url

MIN_DECIDED = 100
THRESHOLD = 0.95

# Each shortcut is one row: (name, SQL condition on jobs, the verdict it would assign).
SHORTCUTS = (
    ("remote + located in Italy, tier 1 -> confirmed",
     "remote_hint = 'remote' AND country = 'italy' AND tier = 1", "confirmed"),
    ("hybrid or onsite, tiers 1/3/4 -> rejected",
     "remote_hint IN ('hybrid', 'onsite') AND tier IN (1, 3, 4)", "rejected"),
    ("remote and open worldwide, tiers 1/3/4 -> confirmed",
     "remote_hint = 'remote_worldwide' AND tier IN (1, 3, 4)", "confirmed"),
)


def hint_agreement(conn) -> list[dict]:
    rows = []
    with conn.cursor() as cur:
        for name, condition, predicted in SHORTCUTS:
            cur.execute(
                f"SELECT remote_verdict, count(*) FROM jobs WHERE {condition} "
                "AND remote_verdict IN ('confirmed', 'rejected', 'unconfirmed') "
                "GROUP BY remote_verdict")
            counts = dict(cur.fetchall())
            agreed = counts.get(predicted, 0)
            other = "rejected" if predicted == "confirmed" else "confirmed"
            decided = agreed + counts.get(other, 0)
            rate = agreed / decided if decided else None
            rows.append({
                "shortcut": name, "agreed": agreed, "disagreed": counts.get(other, 0),
                "unconfirmed": counts.get("unconfirmed", 0), "decided": decided, "rate": rate,
                "ready": decided >= MIN_DECIDED and rate is not None and rate > THRESHOLD,
            })
    return rows


def format_report(rows: list[dict]) -> str:
    lines = []
    for row in rows:
        rate = "n/a" if row["rate"] is None else f"{row['rate']:.1%}"
        verdict = "READY to enable" if row["ready"] else f"not ready (needs {MIN_DECIDED}+ decided and > 95%)"
        lines.append(f"{row['shortcut']}: {rate} of {row['decided']} decided "
                     f"(unconfirmed {row['unconfirmed']}) - {verdict}")
    return "\n".join(lines)


def main() -> int:
    load_dotenv()
    conn = connect(store_url())
    try:
        print(format_report(hint_agreement(conn)))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
