from src.config import load_config
from src.scraper import fetch_offers
from src.dedup import filter_new, mark_seen
from src.scorer import score_offers
from src.obsidian import write_notes, write_digest
from src.telegram import send_summary


def handler(event: dict, context) -> None:
    config = load_config()

    print("[main] Fetching offers...")
    raw_offers = fetch_offers(
        role=config.search.role,
        location=config.search.location,
        time_range=config.search.time_range,
    )
    print(f"[main] Fetched {len(raw_offers)} offers")

    new_offers = filter_new(raw_offers, config.dedup_log_path)
    print(f"[main] {len(new_offers)} new offers after dedup")

    if not new_offers:
        print("[main] No new offers. Exiting.")
        return

    print("[main] Scoring offers...")
    scored = score_offers(
        offers=new_offers,
        profile=config.scoring.candidate_profile,
        priority_keywords=config.scoring.priority_keywords,
        exclude_keywords=config.scoring.exclude_keywords,
        groq_api_key=config.groq_api_key,
    )

    print("[main] Writing to Obsidian vault...")
    write_notes(scored, config.obsidian_vault_path, config.scoring.threshold)
    write_digest(scored, config.obsidian_vault_path, config.scoring.threshold)

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
    handler({}, None)
