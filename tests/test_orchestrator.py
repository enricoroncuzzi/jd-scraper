from unittest.mock import patch, MagicMock, call
import orchestrator


def _make_result(returncode):
    r = MagicMock()
    r.returncode = returncode
    return r


def test_orchestrator_runs_all_four_tiers_in_order(monkeypatch):
    monkeypatch.setattr(orchestrator, "_send_morning_report", lambda _run_id: None)
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
    monkeypatch.setattr(orchestrator, "_send_morning_report", lambda _run_id: None)
    sleep_calls = []
    monkeypatch.setattr("orchestrator.time.sleep", lambda s: sleep_calls.append(s))
    monkeypatch.setattr("orchestrator.subprocess.run", lambda *a, **kw: _make_result(0))

    orchestrator.main()

    assert len(sleep_calls) == 3  # no cooldown after last tier
    assert all(s == orchestrator.COOLDOWN_SECONDS for s in sleep_calls)


def test_orchestrator_continues_after_tier_failure(monkeypatch):
    monkeypatch.setattr(orchestrator, "_send_morning_report", lambda _run_id: None)
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
    monkeypatch.setattr(orchestrator, "_send_morning_report", lambda _run_id: None)
    sleep_calls = []
    monkeypatch.setattr("orchestrator.time.sleep", lambda s: sleep_calls.append(s))
    monkeypatch.setattr("orchestrator.subprocess.run", lambda *a, **kw: _make_result(0))

    orchestrator.main()

    assert len(sleep_calls) == 3  # tiers 1→2, 2→3, 3→4 only


from unittest.mock import patch
import orchestrator


def test_every_tier_gets_the_same_daily_run_id_and_the_report_is_sent_last(monkeypatch):
    seen_env = []
    monkeypatch.setattr(orchestrator, "COOLDOWN_SECONDS", 0)
    with patch("orchestrator.subprocess.run",
               side_effect=lambda cmd, env: seen_env.append(env["JDS_DAILY_RUN_ID"]) or
               type("R", (), {"returncode": 0})()), \
         patch("orchestrator.time.sleep"), \
         patch("orchestrator._send_morning_report") as report:
        orchestrator.main()
    assert len(seen_env) == 4 and len(set(seen_env)) == 1
    report.assert_called_once_with(seen_env[0])


def test_morning_report_is_plain_text_and_split_on_blocks(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    with patch("orchestrator.load_dotenv"), \
         patch("orchestrator.telemetry.drain_buffers"), \
         patch("orchestrator.report_data.load_day", return_value=None), \
         patch("orchestrator.report_render.render_day", return_value=["a_b *c* [d]", "e"]), \
         patch("orchestrator.report_render.pack_messages", return_value=["m1", "m2"]), \
         patch("orchestrator.send_message") as send:
        orchestrator._send_morning_report("day-1")
    assert [c.args[0] for c in send.call_args_list] == ["m1", "m2"]
    assert all(c.kwargs["parse_mode"] is None for c in send.call_args_list)


def test_morning_report_failure_never_raises(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    with patch("orchestrator.load_dotenv"), \
         patch("orchestrator.telemetry.drain_buffers", side_effect=RuntimeError("x")), \
         patch("orchestrator.report_data.load_day", side_effect=RuntimeError("neon down")), \
         patch("orchestrator.send_message", side_effect=RuntimeError("telegram down")):
        orchestrator._send_morning_report("day-1")


def test_a_failed_middle_message_does_not_drop_the_rest(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    with patch("orchestrator.load_dotenv"), \
         patch("orchestrator.telemetry.drain_buffers"), \
         patch("orchestrator.report_data.load_day", return_value=None), \
         patch("orchestrator.report_render.render_day", return_value=["a", "b", "c"]), \
         patch("orchestrator.report_render.pack_messages", return_value=["m1", "m2", "m3"]), \
         patch("orchestrator.send_message", side_effect=[True, RuntimeError("timeout"), False]):
        orchestrator._send_morning_report("day-1")
    out = capsys.readouterr().out
    assert "message(s) 2, 3 of 3 failed" in out
    assert "Morning report sent" not in out


def test_no_database_skips_the_report_quietly(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with patch("orchestrator.load_dotenv"), patch("orchestrator.send_message") as send:
        orchestrator._send_morning_report("day-1")
    send.assert_not_called()
    assert "skipped" in capsys.readouterr().out
