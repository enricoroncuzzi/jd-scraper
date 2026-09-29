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
            rd.StageRow(1, "verification", "groq", "openai/gpt-oss-20b", "openai/gpt-oss-20b",
                        24, 0, 47000),
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
    tier1 = rr.render_day(_report(), settings=SETTINGS)[1]
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
