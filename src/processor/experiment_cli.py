"""Command line for src/processor/experiment.py (wrapped by scripts/llm_experiment.py).

    python scripts/llm_experiment.py sample --from legacy --url "$DATABASE_URL" --days 14 --size 200 --out sample.jsonl
    python scripts/llm_experiment.py remote --sample sample.jsonl --batch-size 8  --max-requests 40   # baseline
    python scripts/llm_experiment.py remote --sample sample.jsonl --batch-size 12 --max-requests 40   # candidate
    python scripts/llm_experiment.py score  --sample sample.jsonl --batch-size 8
    python scripts/llm_experiment.py merged --sample sample.jsonl --batch-size 5

Every subcommand prints a JSON report ending in the adoption decision. Runs spend
real OpenRouter requests (counted, and capped by --max-requests); run them when
the day's budget has room.
"""
import argparse
import json

import psycopg2

from src.processor.combined import build_chain
from src.processor.config import load_processor_config
from src.processor.experiment import (
    decide, install_counter, load_sample, read_sample, run_merged_experiment, run_score_experiment,
    run_verdict_experiment, write_sample,
)
from src.processor.llm_stages import LlmKeys


def _report(name: str, reports: dict) -> int:
    payload = {"experiment": name, **{key: value for key, value in reports.items()}}
    print(json.dumps(payload, indent=2, default=lambda o: o.__dict__))
    return 0


def main(argv: list[str], *, connect=psycopg2.connect, keys=None, chain_factory=build_chain,
         verify=None, score=None) -> int:
    parser = argparse.ArgumentParser(description="Language-model call experiments (spec 6.3, 6.4)")
    sub = parser.add_subparsers(dest="command", required=True)
    sample = sub.add_parser("sample")
    sample.add_argument("--from", dest="source", choices=("legacy", "store"), required=True)
    sample.add_argument("--url", required=True)
    sample.add_argument("--days", type=int, default=14)
    sample.add_argument("--size", type=int, default=200)
    sample.add_argument("--seed", type=int, default=1)
    sample.add_argument("--out", required=True)
    for name in ("remote", "score", "merged"):
        run = sub.add_parser(name)
        run.add_argument("--sample", required=True)
        run.add_argument("--batch-size", type=int, required=True)
        run.add_argument("--max-requests", type=int, default=60)
    args = parser.parse_args(argv)

    if args.command == "sample":
        conn = connect(args.url)
        try:
            jobs = load_sample(conn, args.source, args.days, args.size, args.seed)
        finally:
            conn.close()
        write_sample(args.out, jobs)
        print(f"wrote {len(jobs)} jobs to {args.out}")
        return 0

    cfg = load_processor_config()
    keys = keys or LlmKeys.from_env()
    jobs = read_sample(args.sample)
    counter = install_counter()
    options = dict(batch_size=args.batch_size, counter=counter, max_requests=args.max_requests)
    if args.command == "remote":
        report = run_verdict_experiment(jobs, cfg, keys, **options, **({"verify": verify} if verify else {}))
        return _report("remote", {"verdict": report, "decision": decide(report, None)})
    if args.command == "score":
        report = run_score_experiment(jobs, cfg, keys, **options, **({"score": score} if score else {}))
        return _report("score", {"score": report, "decision": decide(None, report)})
    verdicts, scores = run_merged_experiment(jobs, cfg, keys, **options, chain=chain_factory(keys.llm_api_key))
    return _report("merged", {"verdict": verdicts, "score": scores, "decision": decide(verdicts, scores)})
