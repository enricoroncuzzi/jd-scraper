import sys
import json
import os
import time
import uuid
from datetime import datetime, timezone
from dotenv import load_dotenv
from src.config import load_config
from src.scraper import fetch_offers, refetch_description, resolve_max_pages_per_query
from src.language_filter import filter_by_language
from src.dedup import filter_new, mark_seen
from src.models import JobOffer
from src.retry_queue import build_deferred, read_deferred, save_deferred
from src.scorer import score_offers
from src.remote_verifier import verify_offers, GROQ_DAILY_TOKEN_LIMIT
from src.tier_scope import resolve_allowed_countries
from src.writer import write_notes, write_digest, write_rejected
from src.telegram import send_summary, send_message
from src.storage import save_offers
from src import telemetry
from src.autoapply.pipeline import run_autoapply
from src.retry import run_with_backoff
import tailor as tailor_cli

_USAGE_LOG_PATH = "data/usage_log.jsonl"


def _is_rate_limited(offer: JobOffer) -> bool:
    return offer.description_status == "rate_limited"


def _persist_unfinished(config, previous, expired_rate_limited, pending_rate_limited, unscored):
    """Queue scoring gaps and still-throttled descriptions.

    Returns offers to mark seen (the rate-limit window closed) and how many
    rate-limited descriptions were queued for the next run.
    """
    entries = build_deferred(list(unscored) + list(pending_rate_limited), previous)
    save_deferred(config.retry_queue_path, entries)
    queued_links = {entry.offer.link for entry in entries}
    give_up = [offer for offer in pending_rate_limited if offer.link not in queued_links]
    seen = {offer.link for offer in give_up}
    for entry in expired_rate_limited:
        if entry.offer.link not in queued_links and entry.offer.link not in seen:
            give_up.append(entry.offer)
            seen.add(entry.offer.link)
    rate_limited_queued = sum(1 for entry in entries if _is_rate_limited(entry.offer))
    if rate_limited_queued:
        print(f"[main] {rate_limited_queued} offer(s) deferred because LinkedIn rate-limited "
              f"the description - queued for the next run in {config.retry_queue_path}")
    return give_up, rate_limited_queued


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
    # The per-query page cap is config-driven the same way: each tier config's
    # search.max_pages_per_query sets its own cap, and resolve_max_pages_per_query
    # falls back to the conservative default when it is absent or invalid, so a
    # malformed or older config can never turn into unbounded pagination.
    max_pages_per_query = resolve_max_pages_per_query(config.search.max_pages_per_query)
    raw_offers = fetch_offers(
        roles=config.search.roles,
        location=config.search.location,
        time_range=config.search.time_range,
        work_modes=config.search.work_mode,
        countries=config.search.countries,
        allowed_countries=allowed_countries,
        max_pages_per_query=max_pages_per_query,
    )
    print(f"[main] Fetched {len(raw_offers)} offers")

    language_filtered = filter_by_language(raw_offers)
    print(f"[main] {len(language_filtered)} offers after language filter")

    new_offers = filter_new(language_filtered, config.dedup_log_path)
    print(f"[main] {len(new_offers)} new offers after dedup")
    telemetry.set_fields(offers_fetched=len(raw_offers), offers_new=len(new_offers))

    # Offers an earlier run fetched but never scored, because scoring stopped
    # partway through that tier, or because LinkedIn rate-limited the
    # description. They were deliberately left out of the dedup log, so they
    # come back here instead of being lost for good; expired entries are
    # dropped on load. See src/retry_queue.py.
    loaded = read_deferred(config.retry_queue_path)
    deferred_entries = loaded.live
    expired_rate_limited = loaded.expired_rate_limited
    if deferred_entries:
        print(f"[main] {len(deferred_entries)} deferred offer(s) carried over from an earlier run")

    if not new_offers and not deferred_entries:
        print("[main] No new offers. Exiting.")
        if expired_rate_limited:
            mark_seen([entry.offer for entry in expired_rate_limited], config.dedup_log_path)
        # Nothing live remains. Leaving the expired rows in the file would
        # log and re-mark them on every later empty run.
        save_deferred(config.retry_queue_path, [])
        telemetry.set_fields(
            rate_limit_deferred=0,
            rate_limit_dropped=len(expired_rate_limited),
        )
        return

    ok = sum(1 for o in new_offers if o.description_status == "ok")
    partial = sum(1 for o in new_offers if o.description_status == "partial")
    failed = sum(1 for o in new_offers if o.description_status == "failed")
    rate_limited_count = sum(1 for o in new_offers if _is_rate_limited(o))
    print(f"[main] Description quality - ok: {ok}, partial: {partial}, failed: {failed}, "
          f"rate-limited: {rate_limited_count}")

    # A description LinkedIn refused is not verified or scored on fallback
    # text. Today's copy wins over a queued one, except when today's copy is
    # only rate-limited and the queue already holds a real description: that
    # paid-for copy is what gets scored. A queued rate-limited copy today's
    # scrape did not return is fetched again. The first refetch that is still
    # throttled stops the rest: each full retry ladder is many minutes, and
    # walking all of them would stall the tier.
    queued_by_link = {entry.offer.link: entry for entry in deferred_entries}
    fresh_links: set[str] = set()
    pending_rate_limited: list[JobOffer] = []
    scoreable_new: list[JobOffer] = []
    for offer in new_offers:
        queued = queued_by_link.get(offer.link)
        if _is_rate_limited(offer) and queued is not None and not _is_rate_limited(queued.offer):
            continue
        fresh_links.add(offer.link)
        if _is_rate_limited(offer):
            pending_rate_limited.append(offer)
        else:
            scoreable_new.append(offer)
    carried_ready: list[JobOffer] = []
    carried_to_verify: list[JobOffer] = []
    refetch_blocked = False
    for entry in deferred_entries:
        if entry.offer.link in fresh_links:
            continue
        if _is_rate_limited(entry.offer):
            if refetch_blocked:
                pending_rate_limited.append(entry.offer)
                continue
            refreshed = refetch_description(entry.offer)
            if refreshed.description_status in ("rate_limited", "failed"):
                # A failed refetch has no description either. Store it as
                # rate_limited so the next run tries again on the same clock,
                # and stop the ladder: one dead refetch is enough.
                if refreshed.description_status == "failed":
                    refreshed = refreshed.model_copy(update={
                        "description": "",
                        "description_status": "rate_limited",
                    })
                pending_rate_limited.append(refreshed)
                refetch_blocked = True
            else:
                carried_to_verify.append(refreshed)
        else:
            carried_ready.append(entry.offer)
    if carried_to_verify:
        carried_to_verify = filter_by_language(carried_to_verify)
    # verify_offers keys verdicts by offer id. A carried offer still has the
    # id from the run that fetched it, which collides with today's offers.
    to_verify = _renumber(scoreable_new + carried_to_verify)
    previous = list(deferred_entries) + list(expired_rate_limited)

    verification_degraded = False
    rejected: list = []
    verify_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if config.remote_check.enabled and to_verify:
        print(f"[main] Verifying full-remote status of {len(to_verify)} offers...")
        to_verify, verify_usage = verify_offers(
            offers=to_verify,
            require_italy_eligibility=config.remote_check.require_italy_eligibility,
            groq_api_key=os.environ.get("GROQ_API_KEY", ""),
            llm_api_key=config.llm_api_key,
            groq_tokens_used_today=_verification_usage_today("total_tokens"),
            openrouter_requests_used_today=_verification_usage_today("openrouter_requests"),
        )
        verification_degraded = verify_usage.pop("degraded", False)
        _log_usage(config.tier, "verification", len(to_verify), verify_usage)
        groq_today = _verification_usage_today("total_tokens")
        provider = verify_usage.get("provider", "none")
        provider_note = (" | provider: none (no LLM call made)" if provider == "none"
                         else f" | provider: {provider}")
        if verify_usage.get("openrouter_total_tokens"):
            provider_note += f" (+{verify_usage['openrouter_total_tokens']} OpenRouter tokens)"
        print(f"[main] Verification token usage - prompt: {verify_usage['prompt_tokens']}, "
              f"completion: {verify_usage['completion_tokens']}, total: {verify_usage['total_tokens']}"
              f"{provider_note} | Groq verification total today: {groq_today}/{GROQ_DAILY_TOKEN_LIMIT}")
        if verification_degraded:
            print(f"[main] Verification DEGRADED for tier {config.tier}: a material share of "
                  f"batches never produced a real verdict.")
        rejected = [o for o in to_verify if o.remote_verdict == "rejected"]
        survivors = [o for o in to_verify if o.remote_verdict != "rejected"]
        confirmed = sum(1 for o in survivors if o.remote_verdict == "confirmed")
        print(f"[main] Remote verification - confirmed: {confirmed}, "
              f"unconfirmed: {len(survivors) - confirmed}, rejected: {len(rejected)}")
        telemetry.set_fields(
            verification_provider=verify_usage.get("provider"),
            verification_tokens=verify_usage["total_tokens"] + verify_usage.get("openrouter_total_tokens", 0),
            verification_batches_failed=verify_usage.get("failed_batches", 0),
            verification_batches_total=verify_usage.get("total_batches", 0),
            verification_degraded=verification_degraded,
            verification_confirmed=confirmed,
            verification_unconfirmed=len(survivors) - confirmed,
            verification_rejected=len(rejected),
        )
    else:
        survivors = to_verify

    write_rejected(rejected, config.output_path, config.tier,
                   verification_enabled=config.remote_check.enabled)

    # Scoring deferrals re-enter ahead of today's fresh offers and skip
    # verification: their description and verdict were already paid for.
    # A deferred offer that today's scrape returned again drops out in favour
    # of today's copy, whose verdict is the newer one - including when
    # verification just rejected it. Rate-limited descriptions are not in
    # this list; they stay pending until a fetch succeeds.
    to_score = _renumber(carried_ready + list(survivors))

    if not to_score:
        give_up, rate_limited_queued = _persist_unfinished(
            config, previous, expired_rate_limited, pending_rate_limited, [])
        if rejected:
            print("[main] No offers left to score - all rejected by verification.")
            note = (f" {rate_limited_queued} description(s) were rate-limited and deferred."
                    if rate_limited_queued else "")
            try:
                send_message(
                    f"{config.telegram.greeting}\n\nTier {config.tier}: {len(rejected)} offer(s) found, "
                    f"all {len(rejected)} rejected as not full-remote.{note}",
                    config.telegram_token,
                    config.telegram_chat_id,
                )
            except Exception as notify_exc:
                print(f"[main] Failed to send all-rejected notification: {notify_exc}")
        elif rate_limited_queued:
            print(f"[main] No offers left to score - {rate_limited_queued} rate-limited description(s) deferred.")
            try:
                send_message(
                    f"{config.telegram.greeting}\n\nTier {config.tier}: {rate_limited_queued} offer(s) "
                    f"deferred because LinkedIn rate-limited the description.",
                    config.telegram_token,
                    config.telegram_chat_id,
                )
            except Exception as notify_exc:
                print(f"[main] Failed to send rate-limit deferral notification: {notify_exc}")
        mark_seen(list(rejected) + give_up, config.dedup_log_path)
        telemetry.set_fields(
            rate_limit_deferred=rate_limited_queued,
            rate_limit_dropped=len(give_up),
        )
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
    # Timestamps come from every entry loaded off the queue, including ones
    # this run is about to drop: an offer today's scrape returned again is
    # re-queued as today's copy, but its expiry clock must still run from the
    # first deferral or it can be re-scraped and re-deferred forever.
    give_up, rate_limited_queued = _persist_unfinished(
        config, previous, expired_rate_limited, pending_rate_limited, deferred)
    if deferred:
        print(f"[main] {len(deferred)} offer(s) left unscored (scoring stopped early) "
              f"- queued for the next run in {config.retry_queue_path}")

    telemetry.set_fields(
        prompt_tokens=usage["prompt_tokens"],
        completion_tokens=usage["completion_tokens"],
        total_tokens=usage["total_tokens"],
        offers_deferred=len(deferred),
        rate_limit_deferred=rate_limited_queued,
        rate_limit_dropped=len(give_up),
    )
    if config.db_url:
        session = telemetry.current()
        saved = save_offers(
            config.db_url,
            scored,
            session.run_id if session is not None else None,
            config.tier,
            run_data=session.open_record if session is not None else None,
        )
        if saved is False and session is not None:
            session.storage_failed = True

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
                llm_api_key=config.llm_api_key,
                daily_cap=config.autoapply.daily_cap,
                dry_run=config.autoapply.dry_run,
                telegram_token=config.telegram_token,
                telegram_chat_id=config.telegram_chat_id,
            )
            print(f"[main] Auto-apply packaged {len(packaged)} offer(s)")
            telemetry.set_fields(offers_packaged=len(packaged))
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
    try:
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
    except Exception as e:
        # Every piece of real work (digest, notes, DB rows, retry queue) is
        # finished by the time this runs, so a Telegram outage must degrade the
        # notification rather than the run: an uncaught ConnectionError here
        # used to reach run_tier_with_retry and re-run the whole tier -
        # re-scrape, re-verify, re-score - up to four times, quadrupling a
        # day's LinkedIn requests, Groq tokens and OpenRouter requests over one
        # flaky API minute. Same shape as the auto-apply guard above.
        print(f"[main] Failed to send Telegram summary: {type(e).__name__}: {e}")
        try:
            send_message(
                f"Tier {config.tier} digest FAILED to send: {type(e).__name__}: {e}",
                config.telegram_token,
                config.telegram_chat_id,
            )
        except Exception as notify_exc:
            print(f"[main] Failed to send summary failure notification: {notify_exc}")

    # "Seen" means handled, not fetched: verification rejects, scored offers,
    # and rate-limited descriptions whose carry-over window has closed.
    # Anything still deferred stays new to the next run.
    mark_seen(list(rejected) + list(scored) + give_up, config.dedup_log_path)
    print("[main] Done.")


def _renumber(offers: list[JobOffer]) -> list[JobOffer]:
    """Give this batch unique sequential ids.

    src/scorer.py and src/remote_verifier.py both key results by offer id.
    A deferred offer still carries the id it had on the run that fetched it.
    Without renumbering, those ids collide with today's fresh ones and a
    score or a remote verdict lands on the wrong offer.
    """
    return [offer.model_copy(update={"id": i}) for i, offer in enumerate(offers)]


def _log_usage(tier: int, stage: str, offer_count: int, usage: dict) -> None:
    os.makedirs(os.path.dirname(_USAGE_LOG_PATH), exist_ok=True)
    with open(_USAGE_LOG_PATH, "a") as f:
        f.write(json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tier": tier,
            "stage": stage,
            "offers_scored": offer_count,
            **usage,
        }) + "\n")


def _verification_usage_today(field: str) -> int:
    """Sum `field` over today's verification log entries. "Today" is the UTC
    date because both Groq's and OpenRouter's daily limits reset at the UTC
    day boundary."""
    if not os.path.exists(_USAGE_LOG_PATH):
        return 0
    today = datetime.now(timezone.utc).date().isoformat()
    total = 0
    with open(_USAGE_LOG_PATH) as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("stage") == "verification" and entry.get("timestamp", "").startswith(today):
                total += entry.get(field, 0)
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


def _tier_of(config_path: str) -> int:
    """The tier number, read without load_config so it needs no secrets and never raises."""
    try:
        with open(config_path) as f:
            return int(json.load(f).get("tier", 0))
    except Exception:
        return 0


def run_tier_with_retry(config_path: str, sleep=time.sleep) -> None:
    """Run one tier via handler(), retrying transient failures (an uncaught
    scraper/scorer exception) with exponential backoff. Never retries quota
    exhaustion (see src/retry.py). A final give-up is reported to the captain
    via Telegram, not just left as a cron log line, then re-raised so the
    process still exits non-zero."""

    daily_run_id = os.environ.get("JDS_DAILY_RUN_ID") or uuid.uuid4().hex
    attempts = {"n": 0}

    def attempt():
        attempts["n"] += 1
        telemetry.start_session(
            tier=_tier_of(config_path), daily_run_id=daily_run_id,
            attempt=attempts["n"], db_url=os.environ.get("DATABASE_URL"),
        )
        try:
            handler({}, None, config_path=config_path)
        except BaseException as exc:
            telemetry.end_session(status="failed", error=f"{type(exc).__name__}: {exc}")
            raise
        telemetry.end_session()

    def on_retry(exc: Exception, attempt_num: int, delay: float) -> None:
        print(f"[main] Tier run failed (attempt {attempt_num}): {exc}. Retrying in {delay:.0f}s...")

    def on_give_up(exc: Exception, attempts: int, retryable: bool) -> None:
        reason = "quota exhausted" if not retryable else f"exhausted {attempts} attempt(s)"
        print(f"[main] Tier run giving up ({reason}): {exc}")
        _notify_failure(config_path, exc, attempts, retryable)

    run_with_backoff(attempt, sleep=sleep, on_retry=on_retry, on_give_up=on_give_up)


if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config/config.json"
    load_dotenv()
    run_tier_with_retry(config_path)
