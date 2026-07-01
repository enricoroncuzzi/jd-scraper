from unittest.mock import patch, MagicMock, call
import orchestrator


def _make_result(returncode):
    r = MagicMock()
    r.returncode = returncode
    return r


def test_orchestrator_runs_all_four_tiers_in_order(monkeypatch):
    monkeypatch.setattr("orchestrator.time.sleep", lambda _: None)
    calls = []
    def mock_run(cmd, **kwargs):
        calls.append(cmd)
        return _make_result(0)
    monkeypatch.setattr("orchestrator.subprocess.run", mock_run)

    orchestrator.main()

    assert len(calls) == 4
    for i, config in enumerate(orchestrator.CONFIGS):
        assert config in calls[i]


def test_orchestrator_sleeps_between_tiers(monkeypatch):
    sleep_calls = []
    monkeypatch.setattr("orchestrator.time.sleep", lambda s: sleep_calls.append(s))
    monkeypatch.setattr("orchestrator.subprocess.run", lambda *a, **kw: _make_result(0))

    orchestrator.main()

    assert len(sleep_calls) == 3  # no cooldown after last tier
    assert all(s == orchestrator.COOLDOWN_SECONDS for s in sleep_calls)


def test_orchestrator_continues_after_tier_failure(monkeypatch):
    monkeypatch.setattr("orchestrator.time.sleep", lambda _: None)
    results = [_make_result(1), _make_result(0), _make_result(0), _make_result(0)]
    call_count = {"n": 0}
    def mock_run(cmd, **kwargs):
        r = results[call_count["n"]]
        call_count["n"] += 1
        return r
    monkeypatch.setattr("orchestrator.subprocess.run", mock_run)

    orchestrator.main()  # must not raise

    assert call_count["n"] == 4  # all four tiers attempted


def test_orchestrator_no_cooldown_after_last_tier(monkeypatch):
    sleep_calls = []
    monkeypatch.setattr("orchestrator.time.sleep", lambda s: sleep_calls.append(s))
    monkeypatch.setattr("orchestrator.subprocess.run", lambda *a, **kw: _make_result(0))

    orchestrator.main()

    assert len(sleep_calls) == 3  # tiers 1→2, 2→3, 3→4 only
