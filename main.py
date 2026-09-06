import sys
import json
import os
import time
from datetime import datetime
from src.config import load_config
from src.scraper import fetch_offers
from src.language_filter import filter_by_language
from src.dedup import filter_new, mark_seen
from src.scorer import score_offers
from src.remote_verifier import verify_offers
from src.tier_scope import TIER3_ALLOWED_COUNTRIES
from src.writer import write_notes, write_digest, write_rejected
from src.telegram import send_summary, send_message
from src.storage import init_db, save_run, save_offers
from src.autoapply.pipeline import run_autoapply
from src.retry import run_with_backoff
import tailor as tailor_cli

_USAGE_LOG_PATH = "data/usage_log.jsonl"


def handler(event: dict, context, config_path: str = "config/config.json") -> None:
    config = load_config(config_path)

    print(f"[main] Tier {config.tier} - fetching offers...")
    # The scope filter is bound to the literal tier number 3, not config-driven
    # like every other per-tier knob. Renumbering tiers or adding a fifth tier
    # will silently detach this filter (or attach it to the wrong tier) with no
    # error - the only symptom is out-of-scope roles quietly appearing in the
    # digest. A config-driven binding is filed as separate follow-up work.
    allowed_countries = TIER3_ALLOWED_COUNTRIES if config.tier == 3 else None
    raw_offers = fetch_offers(
        roles=config.search.roles,
        location=config.search.location,
        time_range=config.search.time_range,
        work_modes=config.search.work_mode,
        countries=config.search.countries,
        allowed_countries=allowed_countries,
    )
    print(f"[main] Fetched {len(raw_offers)} offers")

    language_filtered = filter_by_language(raw_offers)
    print(f"[main] {len(language_filtered)} offers after language filter")

    new_offers = filter_new(language_filtered, config.dedup_log_path)
    print(f"[main] {len(new_offers)} new offers after dedup")

    if not new_offers:
        print("[main] No new offers. Exiting.")
        return

    ok = sum(1 for o in new_offers if o.description_status == "ok")
    partial = sum(1 for o in new_offers if o.description_status == "partial")
    failed = sum(1 for o in new_offers if o.description_status == "failed")
    print(f"[main] Description quality - ok: {ok}, partial: {partial}, failed: {failed}")

    verification_degraded = False
    rejected: list = []
    verify_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if config.remote_check.enabled:
        print(f"[main] Verifying full-remote status of {len(new_offers)} offers...")
        new_offers, verify_usage = verify_offers(
            offers=new_offers,
            require_italy_eligibility=config.remote_check.require_italy_eligibility,
            groq_api_key=os.environ.get("GROQ_API_KEY", ""),
        )
        verification_degraded = verify_usage.pop("degraded", False)
        _log_usage(config.tier, "verification", len(new_offers), verify_usage)
        rejected = [o for o in new_offers if o.remote_verdict == "rejected"]
        survivors = [o for o in new_offers if o.remote_verdict != "rejected"]
        confirmed = sum(1 for o in survivors if o.remote_verdict == "confirmed")
        print(f"[main] Remote verification - confirmed: {confirmed}, "
              f"unconfirmed: {len(survivors) - confirmed}, rejected: {len(rejected)}")
    else:
        survivors = new_offers

    write_rejected(rejected, config.output_path, config.tier,
                   verification_enabled=config.remote_check.enabled)

    if not survivors:
        print("[main] No offers left to score - all rejected by verification.")
        try:
            send_message(
                f"{config.telegram.greeting}\n\nTier {config.tier}: {len(rejected)} offer(s) found, "
                f"all {len(rejected)} rejected as not full-remote.",
                config.telegram_token,
                config.telegram_chat_id,
            )
        except Exception as notify_exc:
            print(f"[main] Failed to send all-rejected notification: {notify_exc}")
        if config.db_url:
            try:
                init_db(config.db_url)
                save_run(
                    config.db_url,
                    tier=config.tier,
                    offers_fetched=len(raw_offers),
                    offers_new=len(new_offers),
                    prompt_tokens=verify_usage["prompt_tokens"],
                    completion_tokens=verify_usage["completion_tokens"],
                    total_tokens=verify_usage["total_tokens"],
                )
            except Exception as e:
                print(f"[storage] Failed: {e}")
        mark_seen(new_offers, config.dedup_log_path)
        return

    print("[main] Scoring offers...")
    scored, usage = score_offers(
        offers=survivors,
        profile=config.scoring.candidate_profile,
        priority_keywords=config.scoring.priority_keywords,
        exclude_keywords=config.scoring.exclude_keywords,
        llm_api_key=config.llm_api_key,
    )
    print(f"[main] Token usage - prompt: {usage['prompt_tokens']}, completion: {usage['completion_tokens']}, total: {usage['total_tokens']}")
    _log_usage(config.tier, "scoring", len(survivors), usage)

    if config.db_url:
        try:
            init_db(config.db_url)
            run_id = save_run(
                config.db_url,
                tier=config.tier,
                offers_fetched=len(raw_offers),
                offers_new=len(new_offers),
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                total_tokens=usage["total_tokens"],
            )
            if run_id:
                save_offers(config.db_url, scored, run_id, config.tier)
        except Exception as e:
            print(f"[storage] Failed: {e}")

    print("[main] Writing output files...")
    write_notes(scored, config.output_path, config.scoring.threshold, config.tier)
    write_digest(scored, config.output_path, config.scoring.threshold, tier=config.tier,
                 verification_enabled=config.remote_check.enabled)

    if config.autoapply.enabled:
        mode = "dry-run" if config.autoapply.dry_run else "live"
        print(f"[main] Auto-apply ({mode}): classifying and tailoring above-threshold offers...")
        try:
            packaged = run_autoapply(
                offers=scored,
                threshold=config.scoring.threshold,
                output_path=config.output_path,
                tier=config.tier,
                db_url=config.db_url,
                cv_master_path=tailor_cli._DEFAULT_MASTER,
                css_path=tailor_cli._DEFAULT_CSS,
                groq_api_key=os.environ["GROQ_API_KEY"],
                daily_cap=config.autoapply.daily_cap,
                dry_run=config.autoapply.dry_run,
                telegram_token=config.telegram_token,
                telegram_chat_id=config.telegram_chat_id,
            )
            print(f"[main] Auto-apply packaged {len(packaged)} offer(s)")
        except Exception as e:
            # Never let an auto-apply failure (e.g. a missing CV source path, or a
            # Telegram send failure on notify_package) take down the whole tier run -
            # it degrades to a visible failure notice instead, same as _notify_failure.
            print(f"[main] Auto-apply failed: {type(e).__name__}: {e}")
            try:
                send_message(
                    f"Tier {config.tier} auto-apply FAILED: {type(e).__name__}: {e}",
                    config.telegram_token,
                    config.telegram_chat_id,
                )
            except Exception as notify_exc:
                print(f"[main] Failed to send auto-apply failure notification: {notify_exc}")

    print("[main] Sending Telegram summary...")
    send_summary(
        offers=scored,
        threshold=config.scoring.threshold,
        greeting=config.telegram.greeting,
        token=config.telegram_token,
        chat_id=config.telegram_chat_id,
        verification_enabled=config.remote_check.enabled,
        verification_degraded=verification_degraded,
    )

    mark_seen(new_offers, config.dedup_log_path)
    print("[main] Done.")


def _log_usage(tier: int, stage: str, offer_count: int, usage: dict) -> None:
    os.makedirs(os.path.dirname(_USAGE_LOG_PATH), exist_ok=True)
    with open(_USAGE_LOG_PATH, "a") as f:
        f.write(json.dumps({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "tier": tier,
            "stage": stage,
            "offers_scored": offer_count,
            **usage,
        }) + "\n")


def _notify_failure(config_path: str, exc: Exception, attempts: int, retryable: bool) -> None:
    try:
        config = load_config(config_path)
    except Exception as config_exc:
        print(f"[main] Could not load config to send failure notification: {config_exc}")
        return
    reason = "quota exhausted, not retried" if not retryable else f"failed after {attempts} attempt(s)"
    text = f"Tier {config.tier} run FAILED ({reason})\n{type(exc).__name__}: {exc}"
    try:
        send_message(text, config.telegram_token, config.telegram_chat_id)
    except Exception as notify_exc:
        print(f"[main] Failed to send failure notification: {notify_exc}")


def run_tier_with_retry(config_path: str, sleep=time.sleep) -> None:
    """Run one tier via handler(), retrying transient failures (an uncaught
    scraper/scorer exception) with exponential backoff. Never retries quota
    exhaustion (see src/retry.py). A final give-up is reported to the captain
    via Telegram, not just left as a cron log line, then re-raised so the
    process still exits non-zero."""

    def attempt():
        handler({}, None, config_path=config_path)

    def on_retry(exc: Exception, attempt_num: int, delay: float) -> None:
        print(f"[main] Tier run failed (attempt {attempt_num}): {exc}. Retrying in {delay:.0f}s...")

    def on_give_up(exc: Exception, attempts: int, retryable: bool) -> None:
        reason = "quota exhausted" if not retryable else f"exhausted {attempts} attempt(s)"
        print(f"[main] Tier run giving up ({reason}): {exc}")
        _notify_failure(config_path, exc, attempts, retryable)

    run_with_backoff(attempt, sleep=sleep, on_retry=on_retry, on_give_up=on_give_up)


if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config/config.json"
    run_tier_with_retry(config_path)
