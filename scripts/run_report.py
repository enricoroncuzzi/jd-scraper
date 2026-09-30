"""Read-only run observability report. Run on the server:

    python scripts/run_report.py day [YYYY-MM-DD]
    python scripts/run_report.py trend [--days 14]
    python scripts/run_report.py compare <commit> [--days 60]
    python scripts/run_report.py llm [--stage verification] [--days 7]

Uses the same code as the morning Telegram report, so the two never disagree.
"""
import argparse
import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402
from src import report_data, report_render  # noqa: E402

CONFIGS = [os.path.join(ROOT, f"config/config_tier{n}.json") for n in (1, 2, 3, 4)]


def main() -> int:
    parser = argparse.ArgumentParser(description="jd-scraper run observability report")
    sub = parser.add_subparsers(dest="command", required=True)
    day = sub.add_parser("day")
    day.add_argument("date", nargs="?", type=date.fromisoformat)
    trend = sub.add_parser("trend")
    trend.add_argument("--days", type=int, default=14)
    compare = sub.add_parser("compare")
    compare.add_argument("commit")
    compare.add_argument("--days", type=int, default=60)
    llm = sub.add_parser("llm")
    llm.add_argument("--stage", choices=("scoring", "verification", "tailoring"))
    llm.add_argument("--days", type=int, default=7)
    args = parser.parse_args()

    os.chdir(ROOT)
    load_dotenv()
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return 1
    settings = report_data.load_tier_settings(CONFIGS)

    if args.command == "day":
        report = report_data.load_day(db_url, settings=settings, day=args.date)
        print(report_render.render_day_detail(report, settings=settings))
    elif args.command == "trend":
        since = datetime.now(ZoneInfo("Europe/Rome")).date() - timedelta(days=args.days)
        print(report_render.render_trend(
            report_data.load_daily_metrics(db_url, since=since, settings=settings)))
    elif args.command == "compare":
        since = datetime.now(ZoneInfo("Europe/Rome")).date() - timedelta(days=args.days)
        metrics = report_data.load_daily_metrics(db_url, since=since, settings=settings)
        before, after = report_data.split_before_after(
            metrics, args.commit, report_data.git_is_ancestor)
        print(report_render.render_compare(args.commit, before, after))
    elif args.command == "llm":
        since = datetime.now(ZoneInfo("Europe/Rome")).date() - timedelta(days=args.days)
        models, verdicts = report_data.load_llm_view(db_url, since=since, stage=args.stage)
        print(report_render.render_llm(models, verdicts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
