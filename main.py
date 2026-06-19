import sys
import json
import os
from datetime import datetime
from src.config import load_config
from src.scraper import fetch_offers
from src.language_filter import filter_by_language
from src.dedup import filter_new, mark_seen
from src.scorer import score_offers
from src.writer import write_notes, write_digest
from src.telegram import send_summary

_USAGE_LOG_PATH = "data/usage_log.jsonl"


def handler(event: dict, context, config_path: str = "config/config.json") -> None:
    config = load_config(config_path)

    print(f"[main] Tier {config.tier} — fetching offers...")
    raw_offers = fetch_offers(
        roles=config.search.roles,
        location=config.search.location,
        time_range=config.search.time_range,
        work_modes=config.search.work_mode,
        countries=config.search.countries,
    )
    print(f"[main] Fetched {len(raw_offers)} offers")

    language_filtered = filter_by_language(raw_offers)
    print(f"[main] {len(language_filtered)} offers after language filter")

    new_offers = filter_new(language_filtered, config.dedup_log_path)
    print(f"[main] {len(new_offers)} new offers after dedup")

    if not new_offers:
        print("[main] No new offers. Exiting.")
        return

    print("[main] Scoring offers...")
    scored, usage = score_offers(
        offers=new_offers,
        profile=config.scoring.candidate_profile,
        priority_keywords=config.scoring.priority_keywords,
        exclude_keywords=config.scoring.exclude_keywords,
        llm_api_key=config.llm_api_key,
    )
    print(f"[main] Token usage — prompt: {usage['prompt_tokens']}, completion: {usage['completion_tokens']}, total: {usage['total_tokens']}")
    os.makedirs(os.path.dirname(_USAGE_LOG_PATH), exist_ok=True)
    with open(_USAGE_LOG_PATH, "a") as f:
        f.write(json.dumps({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "tier": config.tier,
            "offers_scored": len(new_offers),
            **usage,
        }) + "\n")

    print("[main] Writing output files...")
    write_notes(scored, config.output_path, config.scoring.threshold, config.tier)
    write_digest(scored, config.output_path, config.scoring.threshold, tier=config.tier)

    print("[main] Sending Telegram summary...")
    send_summary(
        offers=scored,
        threshold=config.scoring.threshold,
        greeting=config.telegram.greeting,
        token=config.telegram_token,
        chat_id=config.telegram_chat_id,
    )

    mark_seen(new_offers, config.dedup_log_path)
    print("[main] Done.")


if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config/config.json"
    handler({}, None, config_path=config_path)
