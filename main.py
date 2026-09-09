import sys
import json
import os
import time
from datetime import datetime
from src.config import load_config
from src.scraper import fetch_offers
from src.language_filter import filter_by_language
from src.dedup import filter_new, mark_seen
from src.models import JobOffer
from src.retry_queue import build_deferred, load_deferred, save_deferred
from src.scorer import score_offers
from src.remote_verifier import verify_offers
from src.tier_scope import resolve_allowed_countries
from src.writer import write_notes, write_digest, write_rejected
from src.telegram import send_summary, send_message
from src.storage import init_db, save_run, save_offers
from src.autoapply.pipeline import run_autoapply
from src.retry import run_with_backoff
import tailor as tailor_cli

_USAGE_LOG_PATH = "data/usage_log.jsonl"
# Groq's free-tier cap (see src/remote_verifier.py's BATCH_SIZE comment) - account-wide,
# not per-key. Printed each run against a running daily total so a human
# reading cron.log can see the budget being approached before a tier dies
# partway through, rather than only after a 429 already truncated a run.
_GROQ_DAILY_TOKEN_LIMIT = 200_000


def handler(event: dict, context, config_path: str = "config/config.json") -> None:
    config = load_config(config_path)

    print(f"[main] Tier {config.tier} - fetching offers...")
    # The allowed-country scope is config-driven: each tier config's
    # search.allowed_countries lists the countries its results must resolve to,
    # and tiers with no list do no geographic narrowing at all.
    # resolve_allowed_countries maps the names onto tier_scope's canonical
    # country vocabulary and raises on a typo, so renumbering tiers or adding a
    # fifth one can no longer silently detach (or attach) this filter.
    allowed_countries = resolve_allowed_countries(config.search.allowed_countries)
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

    # Offers an earlier run fetched but never scored, because scoring stopped
    # partway through that tier. They were deliberately left out of the dedup
    # log, so they come back here instead of being lost for good; expired
    # entries are dropped on load. See src/retry_queue.py.
    deferred_entries = load_deferred(config.retry_queue_path)
    if deferred_entries:
        print(f"[main] {len(deferred_entries)} deferred offer(s) carried over from an earlier run")

    if not new_offers and not deferred_entries:
        print("[main] No new offers. Exiting.")
        return

    ok = sum(1 for o in new_offers if o.description_status == "ok")
    partial = sum(1 for o in new_offers if o.description_status == "partial")
    failed = sum(1 for o in new_offers if o.description_status == "failed")
    print(f"[main] Description quality - ok: {ok}, partial: {partial}, failed: {failed}")

    verification_degraded = False
    rejected: list = []
    verify_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if config.remote_check.enabled and new_offers:
        print(f"[main] Verifying full-remote status of {len(new_offers)} offers...")
        new_offers, verify_usage = verify_offers(
            offers=new_offers,
            require_italy_eligibility=config.remote_check.require_italy_eligibility,
            groq_api_key=os.environ.get("GROQ_API_KEY", ""),
        )
        verification_degraded = verify_usage.pop("degraded", False)
        _log_usage(config.tier, "verification", len(new_offers), verify_usage)
        groq_today = _groq_verification_tokens_today()
        print(f"[main] Verification token usage - prompt: {verify_usage['prompt_tokens']}, "
              f"completion: {verify_usage['completion_tokens']}, total: {verify_usage['total_tokens']} "
              f"| Groq verification total today: {groq_today}/{_GROQ_DAILY_TOKEN_LIMIT}")
        rejected = [o for o in new_offers if o.remote_verdict == "rejected"]
        survivors = [o for o in new_offers if o.remote_verdict != "rejected"]
        confirmed = sum(1 for o in survivors if o.remote_verdict == "confirmed")
        print(f"[main] Remote verification - confirmed: {confirmed}, "
              f"unconfirmed: {len(survivors) - confirmed}, rejected: {len(rejected)}")
    else:
        survivors = new_offers

    write_rejected(rejected, config.output_path, config.tier,
                   verification_enabled=config.remote_check.enabled)

    # Deferred offers re-enter here, ahead of today's fresh ones, and skip
    # verification: their description and verdict were already paid for on the
    # run that fetched them. A deferred offer that today's scrape returned
    # again drops out in favour of today's copy, whose verdict is the newer one
    # - including when verification just rejected it.
    fresh_links = {o.link for o in new_offers}
    carried = [entry for entry in deferred_entries if entry.offer.link not in fresh_links]
    to_score = _renumber([entry.offer for entry in carried] + list(survivors))

    if not to_score:
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
        # Nothing is pending on this path (a non-empty `carried` would have
        # scored), so whatever the queue file still holds is superseded by
        # today's copies or expired. This branch never reaches the rewrite
        # below, so clear it here.
        save_deferred(config.retry_queue_path, [])
        return

    print("[main] Scoring offers...")
    scored, usage = score_offers(
        offers=to_score,
        profile=config.scoring.candidate_profile,
        priority_keywords=config.scoring.priority_keywords,
        exclude_keywords=config.scoring.exclude_keywords,
        llm_api_key=config.llm_api_key,
    )
    print(f"[main] Token usage - prompt: {usage['prompt_tokens']}, completion: {usage['completion_tokens']}, total: {usage['total_tokens']}")
    _log_usage(config.tier, "scoring", len(to_score), usage)

    # score_offers returns what it scored and stops when a batch dies after
    # all retries (quota exhaustion, an upstream 5xx, an empty structured
    # output). Whatever it never reached is NOT handled, so it must not go on
    # the dedup log and must go back on the queue - otherwise it can never be
    # seen again. This is how 222 of 242 tier-4 offers vanished on 2026-09-07.
    scored_links = {o.link for o in scored}
    deferred = [o for o in to_score if o.link not in scored_links]
    save_deferred(config.retry_queue_path, build_deferred(deferred, carried))
    if deferred:
        print(f"[main] {len(deferred)} offer(s) left unscored (scoring stopped early) "
              f"- queued for the next run in {config.retry_queue_path}")

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
                 verification_enabled=config.remote_check.enabled,
                 deferred_count=len(deferred))

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
        deferred_count=len(deferred),
    )

    # "Seen" means handled, not fetched: only the offers verification rejected
    # and the offers scoring actually scored are recorded, so anything deferred
    # above is still new to the next run.
    mark_seen(list(rejected) + list(scored), config.dedup_log_path)
    print("[main] Done.")


def _renumber(offers: list[JobOffer]) -> list[JobOffer]:
    """Give the scoring input unique sequential ids.

    src/scorer.py keys its results by offer id, and a deferred offer still
    carries the id it had on the run that fetched it. Without renumbering,
    those ids collide with today's fresh ones and scores land on the wrong
    offers.
    """
    return [offer.model_copy(update={"id": i}) for i, offer in enumerate(offers)]


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


def _groq_verification_tokens_today() -> int:
    if not os.path.exists(_USAGE_LOG_PATH):
        return 0
    today = datetime.now().date().isoformat()
    total = 0
    with open(_USAGE_LOG_PATH) as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("stage") == "verification" and entry.get("timestamp", "").startswith(today):
                total += entry.get("total_tokens", 0)
    return total


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
