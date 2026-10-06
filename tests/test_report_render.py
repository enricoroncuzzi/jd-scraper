from datetime import datetime, timezone
from src import report_data as rd
from src import report_render as rr

T0 = datetime(2026, 10, 5, 5, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 10, 5, 8, 41, tzinfo=timezone.utc)
SETTINGS = {1: rd.TierSettings(8, True), 2: rd.TierSettings(8, False),
            3: rd.TierSettings(8, True), 4: rd.TierSettings(8, True)}


def _tier(n, **kw):
    base = dict(tier=n, run_uuid=f"u{n}", run_id=n, attempt=1, attempts=1, status="ok",
                started_at=T0, finished_at=T1, error=None, git_commit="c" * 40, git_dirty=False,
                offers_fetched=100, offers_packaged=2, verification_provider="groq",
                verification_tokens=1000, verification_batches_failed=0,
                verification_batches_total=5, verification_degraded=False, telemetry_ok=True,
                scored=40, high=4)
    base.update(kw)
    return rd.TierRun(**base)


def _report(tiers=None, **kw):
    base = dict(
        daily_run_id="d", started_at=T0, finished_at=T1, git_commit="c" * 40, git_dirty=False,
        previous_commit="c" * 40, commit_subject=None,
        tiers=tiers if tiers is not None else {n: _tier(n) for n in (1, 2, 3, 4)},
        queries=[rd.QueryRow(3, "AI Engineer", "Europe", "remote", 20, 20, 200, 180, "cap_hit")],
        stages=[
            rd.StageRow(1, "scoring", "openrouter", "nvidia/nemotron-3-super-120b-a12b:free",
                        "nvidia/nemotron-3-super-120b-a12b:free", 12, 0, 40000),
            rd.StageRow(1, "scoring", "openrouter", "nvidia/nemotron-3-super-120b-a12b:free",
                        "liquid/lfm-2.5-2.6b:free", 1, 0, 3000),
            rd.StageRow(1, "verification", "openrouter", "nvidia/nemotron-3-super-120b-a12b:free",
                        "nvidia/nemotron-3-super-120b-a12b:free", 24, 0, 47000),
        ],
        limits=[
            rd.LimitUse("groq", "openai/gpt-oss-20b", "tokens", 200000, 134000,
                        {1: 47000, 2: 47000, 3: 90000, 4: 134000}),
            rd.LimitUse("openrouter", "*", "requests", 1000, 122, {1: 13, 2: 31, 3: 70, 4: 122}),
            rd.LimitUse("groq", "openai/gpt-oss-120b", "tokens", None, 70000,
                        {1: 21000, 2: 49000, 3: 60000, 4: 70000}),
        ],
        llm_calls=131, llm_failed=2, p95_latency_ms=4100.0,
    )
    base.update(kw)
    return rd.DayReport(**base)


def test_healthy_day_is_all_ok_and_has_no_warnings():
    blocks = rr.render_day(_report(), settings=SETTINGS)
    assert len(blocks) == 5
    assert "ALL OK" in blocks[0] and "⚠" not in blocks[0]
    assert "134k / 200k tokens (67%)" in blocks[0]
    assert "122 / 1000 requests (12%)" in blocks[0]
    assert "70k / ? tokens" in blocks[0]  # unknown limit: shown, never guessed or divided


def test_fallback_model_appears_next_to_the_primary_with_counts():
    tier1 = rr.render_day(_report(), settings=SETTINGS)[1]
    assert "nemotron-3-super-120b-a12b (12 calls) + lfm-2.5-2.6b (1 call)" in tier1


def test_each_limit_is_shown_in_its_own_unit_per_tier():
    # Groq is the verification fallback. This block is built on its own so the
    # healthy-day fixture can show the OpenRouter primary without hiding the
    # token-limit line.
    stages = [
        rd.StageRow(1, "scoring", "openrouter", "nvidia/nemotron-3-super-120b-a12b:free",
                    "nvidia/nemotron-3-super-120b-a12b:free", 12, 0, 40000),
        rd.StageRow(1, "verification", "groq", "openai/gpt-oss-20b", "openai/gpt-oss-20b",
                    24, 0, 47000),
    ]
    tier1 = rr.render_day(_report(stages=stages), settings=SETTINGS)[1]
    assert "day so far 13/1000 req" in tier1
    assert "day so far 47k/200k tok" in tier1


def test_tier_without_verification_says_so():
    assert "Verification  not used on this tier" in rr.render_day(_report(), settings=SETTINGS)[2]


def test_crashed_failed_retried_and_missing_tiers_all_warn():
    tiers = {1: _tier(1, status="running", finished_at=None),
             2: _tier(2, status="failed", error="RuntimeError: LinkedIn 403"),
             3: _tier(3, attempts=2, attempt=2)}
    warnings = rr.collect_warnings(_report(tiers=tiers), settings=SETTINGS)
    joined = "\n".join(warnings)
    assert "Tier 1 (Italy) CRASHED" in joined
    assert "Tier 2 (Switzerland) FAILED: RuntimeError: LinkedIn 403" in joined
    assert "Tier 3 (EU) needed 2 attempts" in joined
    assert "Tier 4 (UK) NOT RECORDED" in joined
    blocks = rr.render_day(_report(tiers=tiers), settings=SETTINGS)
    assert "1 of 4 tiers ok" in blocks[0]
    assert "NOT RECORDED" in blocks[4]


def test_limit_past_90_percent_warns():
    limits = [rd.LimitUse("groq", "openai/gpt-oss-20b", "tokens", 200000, 191000, {1: 191000})]
    warnings = rr.collect_warnings(_report(limits=limits), settings=SETTINGS)
    assert any("96%" in w for w in warnings)


def test_new_code_dirty_copy_degraded_and_incomplete_telemetry_warn():
    tiers = {n: _tier(n) for n in (1, 2, 3, 4)}
    tiers[3] = _tier(3, verification_degraded=True, verification_batches_failed=4,
                     verification_batches_total=20, status="degraded")
    tiers[4] = _tier(4, telemetry_ok=False)
    report = _report(tiers=tiers, previous_commit="b" * 40, commit_subject="per-tier page cap",
                     git_dirty=True)
    joined = "\n".join(rr.collect_warnings(report, settings=SETTINGS))
    assert "New code live since the last run: cccccccc \"per-tier page cap\"" in joined
    assert "uncommitted edits" in joined
    assert "Tier 3 (EU) verification DEGRADED: 4 of 20 batches failed" in joined
    assert "Tier 4 (UK) telemetry incomplete" in joined


def test_no_report_at_all_is_itself_an_alarm():
    [block] = rr.render_day(None, settings=SETTINGS)
    assert "no telemetry was recorded" in block


def test_pack_messages_combines_small_blocks_and_never_splits_a_block():
    blocks = ["a" * 1000, "b" * 1000, "c" * 3000]
    assert rr.pack_messages(blocks, limit=4096) == ["a" * 1000 + "\n\n" + "b" * 1000, "c" * 3000]


def test_pack_messages_splits_an_oversized_block_without_exceeding_or_dropping():
    huge = "\n".join("x" * 100 for _ in range(100))  # ~10k chars
    out = rr.pack_messages(["head", huge, "tail"], limit=4096)
    assert all(len(m) <= 4096 for m in out)
    assert out[0].startswith("head") and out[-1].endswith("tail")
    assert "".join(out).count("x") == 100 * 100


def test_pack_messages_cuts_a_single_unbroken_line():
    out = rr.pack_messages(["y" * 9000], limit=4096)
    assert all(len(m) <= 4096 for m in out) and "".join(out) == "y" * 9000


def test_helpers():
    assert rr.short_model("nvidia/nemotron-3-super-120b-a12b:free") == "nemotron-3-super-120b-a12b"
    assert rr.short_model("openai/gpt-oss-20b") == "gpt-oss-20b"
    assert rr.fmt_tokens(None) == "-" and rr.fmt_tokens(999) == "999" and rr.fmt_tokens(134400) == "134k"


def test_a_health_warning_blocks_all_ok():
    tiers = {n: _tier(n, telemetry_ok=False) for n in (1, 2, 3, 4)}
    block = rr.render_day(_report(tiers=tiers), settings=SETTINGS)[0]
    assert "ALL OK" not in block
    assert "CHECK" in block
    assert "telemetry incomplete" in block


def test_new_code_alone_does_not_block_all_ok():
    report = _report(previous_commit="b" * 40, commit_subject="per-tier page cap")
    block = rr.render_day(report, settings=SETTINGS)[0]
    assert "ALL OK" in block
    assert "New code live" in block


def test_detail_lists_every_attempt():
    log = [
        rd.AttemptView(1, 1, 2, "failed", T0, T1, "RuntimeError: first"),
        rd.AttemptView(1, 2, 2, "ok", T0, T1, None),
    ]
    text = rr.render_day_detail(_report(attempt_log=log), settings=SETTINGS)
    assert "T1 attempt 1 of 2 · failed" in text
    assert "error: RuntimeError: first" in text
    assert "T1 attempt 2 of 2 · ok" in text


def test_detail_marks_searches_that_reused_an_earlier_fetch():
    reused = rd.QueryRow(3, "ML Engineer", "Europe", "remote", 5, 20, 50, 40, "empty_page",
                         cross_query_duplicates=12)
    plain = rd.QueryRow(3, "AI Engineer", "Europe", "remote", 5, 20, 50, 40, "empty_page")
    text = rr.render_day_detail(_report(queries=[plain, reused]), settings=SETTINGS)
    assert "40 kept (12 reused from an earlier search), stopped: empty_page" in text
    assert "AI Engineer / Europe / remote: 5/20 pages, 50 cards, 40 kept, stopped: empty_page" in text


def test_llm_view_shows_generic_errors():
    text = rr.render_llm([{
        "stage": "scoring", "model": "no response", "calls": 3,
        "ok": 0, "rate_limited": 0, "quota_exhausted": 0, "invalid_output": 0,
        "timeout": 0, "error": 3, "p50": 0, "p95": 0, "avg_in": None, "avg_out": None,
    }], [])
    assert "error" in text.splitlines()[0]
    assert "no response" in text
    assert "100%" in text
    assert "   0%" in text or "  0%" in text


def test_share_label_follows_loaded_tier_settings():
    one = {1: rd.TierSettings(threshold=6), 2: rd.TierSettings(threshold=6)}
    mixed = {1: rd.TierSettings(threshold=6), 2: rd.TierSettings(threshold=7)}
    assert "6+ share" in rr.render_trend([], settings=one)
    assert ">=threshold share" in rr.render_trend([], settings=mixed)
    assert "Share scoring 6+ (%)" in rr.render_compare("abc", [], [], settings=one)
    assert "Share scoring >=threshold (%)" in rr.render_compare("abc", [], [], settings=mixed)


def test_compare_includes_llm_failure_rate():
    from datetime import date
    def day(n, failed):
        return rd.DailyMetrics(
            day=date(2026, 10, n), commits=set(), offers_fetched=10, cap_hits=0, queries=2,
            verification_tokens=100, scored=4, high=1, llm_calls=10, llm_failed=failed, packaged=1)
    text = rr.render_compare("abcdef1234", [day(1, 1)], [day(2, 4)])
    assert "LLM failure rate (%)" in text


def test_day_report_shows_linkedin_429s_and_rate_limit_deferrals_per_tier():
    tiers = {n: _tier(n, search_rate_limits=0, description_rate_limits=0, rate_limit_deferred=0)
             for n in (1, 2, 3, 4)}
    tiers[3] = _tier(3, search_rate_limits=4, description_rate_limits=11,
                     rate_limit_deferred=3, offers_fetched=100)
    blocks = rr.render_day(_report(tiers=tiers), settings=SETTINGS)
    assert "LinkedIn 429s: 4 search, 11 description, 3 deferred" in blocks[3]
    assert "ALL OK" in blocks[0]


def test_rate_limit_deferrals_and_429s_past_the_threshold_block_all_ok():
    fetched = 100
    over_share = int(fetched * rr.RATE_LIMIT_DEFER_SHARE_WARN) + 1
    tiers = {n: _tier(n, search_rate_limits=0, description_rate_limits=0, rate_limit_deferred=0)
             for n in (1, 2, 3, 4)}
    tiers[4] = _tier(4, offers_fetched=fetched, rate_limit_deferred=over_share,
                     search_rate_limits=0, description_rate_limits=0)
    block = rr.render_day(_report(tiers=tiers), settings=SETTINGS)[0]
    assert "ALL OK" not in block
    assert "CHECK" in block
    assert "deferrals" in block
    assert f"{over_share} of {fetched}" in block

    at_share = int(fetched * rr.RATE_LIMIT_DEFER_SHARE_WARN)
    tiers[4] = _tier(4, offers_fetched=fetched, rate_limit_deferred=at_share,
                     search_rate_limits=0, description_rate_limits=0)
    assert "ALL OK" in rr.render_day(_report(tiers=tiers), settings=SETTINGS)[0]

    tiers[4] = _tier(4, offers_fetched=fetched, rate_limit_deferred=0,
                     search_rate_limits=rr.LINKEDIN_429_WARN, description_rate_limits=0)
    assert "ALL OK" in rr.render_day(_report(tiers=tiers), settings=SETTINGS)[0]
    tiers[4] = _tier(4, offers_fetched=fetched, rate_limit_deferred=0,
                     search_rate_limits=rr.LINKEDIN_429_WARN, description_rate_limits=1)
    block = rr.render_day(_report(tiers=tiers), settings=SETTINGS)[0]
    assert "ALL OK" not in block
    assert "429" in block
    assert str(rr.LINKEDIN_429_WARN + 1) in block


def test_trend_includes_rate_limit_total_and_deferred_count():
    from datetime import date
    metrics = [rd.DailyMetrics(
        day=date(2026, 10, 4), commits=set(), offers_fetched=10, cap_hits=2, queries=8,
        verification_tokens=100, scored=4, high=1, llm_calls=10, llm_failed=0, packaged=1,
        rate_limits=112, rate_limit_deferred=7, rate_limit_dropped=2,
    )]
    text = rr.render_trend(metrics, settings=SETTINGS)
    header, row = text.splitlines()
    assert "429s" in header and "rl-defer" in header and "rl-drop" in header
    assert row.split()[-3:] == ["112", "7", "2"]


def test_a_rate_limit_give_up_blocks_all_ok():
    tiers = {n: _tier(n, search_rate_limits=0, description_rate_limits=0,
                      rate_limit_deferred=0, rate_limit_dropped=0)
             for n in (1, 2, 3, 4)}
    assert "ALL OK" in rr.render_day(_report(tiers=tiers), settings=SETTINGS)[0]
    tiers[3] = _tier(3, offers_fetched=100, search_rate_limits=0, description_rate_limits=0,
                     rate_limit_deferred=0, rate_limit_dropped=40)
    blocks = rr.render_day(_report(tiers=tiers), settings=SETTINGS)
    assert "ALL OK" not in blocks[0]
    assert "rate-limit give-ups: 40" in blocks[0]
    assert f"warn above {rr.RATE_LIMIT_DROPPED_WARN}" in blocks[0]
    assert "40 dropped" in blocks[3]


_NEMOTRON = "nvidia/nemotron-3-super-120b-a12b:free"
_LIQUID = "liquid/lfm-2.5-2.6b:free"
_DOTS = "dots-studio/dots-3-note-preview:free"


def test_answering_model_other_than_the_configured_primary_blocks_all_ok():
    stages = [
        rd.StageRow(1, "scoring", "openrouter", _NEMOTRON, _NEMOTRON, 7, 0, 1000),
        rd.StageRow(1, "scoring", "openrouter", _NEMOTRON, _LIQUID, 2, 0, 100),
        rd.StageRow(1, "scoring", "openrouter", _NEMOTRON, _DOTS, 1, 0, 50),
        rd.StageRow(2, "tailoring", "openrouter", _NEMOTRON, _LIQUID, 4, 0, 10),
    ]
    block = rr.render_day(_report(stages=stages), settings=SETTINGS)[0]
    assert "ALL OK" not in block
    assert "CHECK" in block
    assert "Tier 1 (Italy) Scoring: 3 of 10 calls answered by lfm-2.5-2.6b (2), dots-3-note-preview (1)" in block
    assert "Tier 2 (Switzerland) Tailoring: 4 of 4 calls answered by lfm-2.5-2.6b (4)" in block
    assert f"warn above {round(100 * rr.PRIMARY_MODEL_MISMATCH_SHARE_WARN)}%" in block
    assert "primary nemotron-3-super-120b-a12b" in block
    detail = rr.render_day_detail(_report(stages=stages), settings=SETTINGS)
    assert "Tier 1 (Italy) Scoring: 3 of 10 calls answered by lfm-2.5-2.6b (2), dots-3-note-preview (1)" in detail


def test_primary_model_share_at_the_threshold_stays_all_ok():
    stages = [
        rd.StageRow(1, "scoring", "openrouter", _NEMOTRON, _NEMOTRON, 8, 0, 1000),
        rd.StageRow(1, "scoring", "openrouter", _NEMOTRON, _LIQUID, 2, 0, 100),
        rd.StageRow(1, "verification", "openrouter", _NEMOTRON, _NEMOTRON, 10, 0, 100),
    ]
    assert "ALL OK" in rr.render_day(_report(stages=stages), settings=SETTINGS)[0]


def test_failed_and_unanswered_calls_do_not_count_as_another_model():
    stages = [
        rd.StageRow(1, "scoring", "openrouter", _NEMOTRON, _NEMOTRON, 8, 0, 1000),
        rd.StageRow(1, "scoring", "openrouter", _NEMOTRON, _LIQUID, 10, 8, 100),
        rd.StageRow(1, "scoring", "openrouter", _NEMOTRON, rd.NO_RESPONSE_MODEL, 20, 20, 0),
        rd.StageRow(1, "verification", "openrouter", _NEMOTRON, _NEMOTRON, 4, 0, 100),
    ]
    assert "ALL OK" in rr.render_day(_report(stages=stages), settings=SETTINGS)[0]


def test_one_fallback_call_on_a_short_stage_stays_all_ok():
    stages = [
        rd.StageRow(1, "scoring", "openrouter", _NEMOTRON, _NEMOTRON, 25, 0, 1000),
        rd.StageRow(1, "verification", "openrouter", _NEMOTRON, _NEMOTRON, 28, 0, 1000),
        rd.StageRow(1, "tailoring", "openrouter", _NEMOTRON, _LIQUID, 1, 0, 10),
    ]
    block = rr.render_day(_report(stages=stages), settings=SETTINGS)[0]
    assert "ALL OK" in block
    assert "answered by" not in block


def test_verification_answered_by_groq_instead_of_the_primary_blocks_all_ok():
    stages = [
        rd.StageRow(4, "verification", "groq", "openai/gpt-oss-20b", "openai/gpt-oss-20b",
                    24, 0, 47000),
        rd.StageRow(4, "scoring", "openrouter", _NEMOTRON, _NEMOTRON, 10, 0, 1000),
    ]
    block = rr.render_day(_report(stages=stages), settings=SETTINGS)[0]
    assert "ALL OK" not in block
    assert "Tier 4 (UK) Verification: 24 of 24 calls answered by gpt-oss-20b" in block


def test_unconfigured_limits_render_as_unknown_and_warn():
    from src.report_data import LimitUse
    limits = [
        LimitUse("groq", "openai/gpt-oss-20b", "tokens", None, 10, {}),
        LimitUse("groq", "openai/gpt-oss-120b", "tokens", None, 0, {}),
        LimitUse("openrouter", "*", "requests", None, 0, {}),
    ]
    report = _report(limits=limits, limits_unconfigured=True)
    block = rr.render_day(report, settings=SETTINGS)[0]
    assert block.count("/ ? ") >= 2
    assert "Provider limits could not be loaded" in block
    assert "ALL OK" not in block
