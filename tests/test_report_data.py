import json
from datetime import date
from src import report_data as rd


def test_load_tier_settings_reads_threshold_and_verification(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"tier": 2, "scoring": {"threshold": 7}, "remote_check": {"enabled": False}}))
    q = tmp_path / "bad.json"
    q.write_text("{nope")
    assert rd.load_tier_settings([str(p), str(q)]) == {2: rd.TierSettings(7, False)}


def test_shipped_tier_configs_parse():
    s = rd.load_tier_settings([f"config/config_tier{n}.json" for n in (1, 2, 3, 4)])
    assert set(s) == {1, 2, 3, 4}
    assert s[2].verification_enabled is False and s[1].verification_enabled is True


def _m(day, commits):
    return rd.DailyMetrics(day=day, commits=set(commits), offers_fetched=1, cap_hits=None,
                           queries=None, verification_tokens=None, scored=1, high=0,
                           llm_calls=None, llm_failed=None, packaged=0)


def test_split_before_after_uses_first_day_running_the_commit_or_a_descendant():
    days = [_m(date(2026, 10, 1), []), _m(date(2026, 10, 2), ["old"]),
            _m(date(2026, 10, 3), ["new"]), _m(date(2026, 10, 4), ["newer"])]
    ancestry = {("new", "new"), ("new", "newer")}
    before, after = rd.split_before_after(days, "new", lambda a, d: (a, d) in ancestry)
    assert [m.day.day for m in before] == [1, 2]
    assert [m.day.day for m in after] == [3, 4]


def test_split_with_no_day_running_the_commit_puts_everything_before():
    days = [_m(date(2026, 10, 1), ["x"])]
    before, after = rd.split_before_after(days, "new", lambda a, d: False)
    assert len(before) == 1 and after == []


def test_git_helpers_never_raise(tmp_path):
    assert rd.git_is_ancestor("deadbeef", "cafebabe", repo_dir=str(tmp_path)) is False
    assert rd.git_subject("deadbeef", repo_dir=str(tmp_path)) is None


def test_utc_day_bounds_split_on_midnight():
    from datetime import datetime, timezone
    before = datetime(2026, 10, 5, 23, 55, tzinfo=timezone.utc)
    after = datetime(2026, 10, 6, 0, 10, tzinfo=timezone.utc)
    start_before, end_before = rd.utc_day_bounds(before)
    start_after, end_after = rd.utc_day_bounds(after)
    assert start_before == datetime(2026, 10, 5, tzinfo=timezone.utc)
    assert end_before == start_after
    assert end_after == datetime(2026, 10, 7, tzinfo=timezone.utc)
    assert start_before != start_after


def test_missing_limits_catalog_is_three_unknown_ceilings(monkeypatch):
    monkeypatch.setattr(rd, "load_limits", lambda: [])
    limits, unconfigured = rd.catalog_for_report()
    assert unconfigured is True
    assert [(l.provider, l.model, l.per_day) for l in limits] == [
        ("groq", "openai/gpt-oss-20b", None),
        ("groq", "openai/gpt-oss-120b", None),
        ("openrouter", "*", None),
    ]


def test_a_real_catalog_is_not_replaced(monkeypatch):
    from src.llm_limits import Limit
    shipped = [Limit("groq", "openai/gpt-oss-20b", "tokens", 200000)]
    monkeypatch.setattr(rd, "load_limits", lambda: shipped)
    limits, unconfigured = rd.catalog_for_report()
    assert unconfigured is False and limits == shipped


def test_unanswered_calls_are_not_attributed_to_the_requested_model():
    import inspect
    day = inspect.getsource(rd.load_day)
    view = inspect.getsource(rd.load_llm_view)
    assert "COALESCE(c.response_model, c.request_model)" not in day
    assert "COALESCE(response_model, request_model)" not in view
    assert "NO_RESPONSE_MODEL" in day and "NO_RESPONSE_MODEL" in view
    assert rd.NO_RESPONSE_MODEL == "no response"


def test_report_connect_is_bounded():
    from unittest.mock import patch
    with patch("src.report_data.psycopg2.connect") as connect:
        rd._connect("postgresql://x")
    assert connect.call_args.kwargs["connect_timeout"] == 10
    assert "statement_timeout" in connect.call_args.kwargs["options"]
