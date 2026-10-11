"""Label scraped postings as golden / not_for_me.

    python label.py --uri jdlabel:golden/<date>/tier<N>/scraped/<note>.md
    python label.py --remove golden <link-or-note-path>
    python label.py --list golden
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from src import labels
from src.tailor.notify import notify

_DEFAULT_ROOT = os.environ.get("JD_OUTPUT_ROOT", "/Users/enricoroncuzzi/Desktop/raw/work/jd-output")
_USAGE = (
    "Usage: python label.py --uri <jdlabel:...> | --remove <golden|not_for_me> <link-or-note> "
    "| --list <golden|not_for_me>"
)


def _db_url() -> str | None:
    return os.environ.get("DATABASE_URL")


def _link_for(target: str, root: str) -> str:
    if target.startswith(("http://", "https://")):
        return target
    path = target if os.path.isabs(target) else labels.resolve_note(target, root)
    return labels.read_note(path)["link"]


def handle_uri(uri: str, root: str, db_url: str | None) -> str:
    try:
        label, note_rel = labels.parse_label_uri(uri)
        info = labels.read_note(labels.resolve_note(note_rel, root))
        result = labels.set_label(db_url, info["link"], label, info["note"])
    except Exception as e:
        notify("Label failed", str(e)[:150])
        raise
    name = f"{info['title']} - {info['company']}"
    if result["created"]:
        msg = f"{name}: {labels.ICON[label]} saved to {labels.DISPLAY[label]} set"
        if result["replaced"]:
            msg += f" (replaced {labels.DISPLAY[result['replaced']]})"
    else:
        msg = f"{name}: already in {labels.DISPLAY[label]} set"
    notify("Offer labelled", msg)
    return msg


def remove(label: str, target: str, root: str, db_url: str | None) -> str:
    removed = labels.remove_label(db_url, _link_for(target, root), label)
    return f"removed {label} label" if removed else f"no {label} label on that posting"


def list_golden(label: str, db_url: str | None) -> list[str]:
    lines = []
    for r in labels.list_labels(db_url, label):
        when = r["labeled_at"].date().isoformat() if r["labeled_at"] else "-"
        lines.append(
            f"{r['title'] or '-'} | {r['company'] or '-'} | {r['score'] if r['score'] is not None else '-'}"
            f" | {when} | {r['link']}"
        )
    return lines


def main(argv: list[str]) -> int:
    try:
        if len(argv) >= 2 and argv[0] == "--uri":
            print(handle_uri(argv[1], _DEFAULT_ROOT, _db_url()))
        elif len(argv) >= 3 and argv[0] == "--remove":
            print(remove(argv[1], argv[2], _DEFAULT_ROOT, _db_url()))
        elif len(argv) >= 2 and argv[0] == "--list":
            print("\n".join(list_golden(argv[1], _db_url())))
        else:
            print(_USAGE)
            return 1
    except Exception as e:
        print(f"[label] {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
