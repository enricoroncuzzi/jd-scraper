from unittest.mock import patch, MagicMock
import orchestrator


def _make_result(returncode):
    r = MagicMock()
    r.returncode = returncode
    return r


def test_orchestrator_runs_all_four_tiers_in_order(monkeypatch):
    monkeypatch.setattr(orchestrator, "_send_day_summary", lambda _run_id: None)
    monkeypatch.setattr(orchestrator, "_send_tier_report", lambda _tier, _run_id: None)
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
    monkeypatch.setattr(orchestrator, "_send_day_summary", lambda _run_id: None)
    monkeypatch.setattr(orchestrator, "_send_tier_report", lambda _tier, _run_id: None)
    sleep_calls = []
    monkeypatch.setattr("orchestrator.time.sleep", lambda s: sleep_calls.append(s))
    monkeypatch.setattr("orchestrator.subprocess.run", lambda *a, **kw: _make_result(0))

    orchestrator.main()

    assert len(sleep_calls) == 3  # no cooldown after last tier
    assert all(s == orchestrator.COOLDOWN_SECONDS for s in sleep_calls)


def test_orchestrator_continues_after_tier_failure(monkeypatch):
    monkeypatch.setattr(orchestrator, "_send_day_summary", lambda _run_id: None)
    monkeypatch.setattr(orchestrator, "_send_tier_report", lambda _tier, _run_id: None)
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
    monkeypatch.setattr(orchestrator, "_send_day_summary", lambda _run_id: None)
    monkeypatch.setattr(orchestrator, "_send_tier_report", lambda _tier, _run_id: None)
    sleep_calls = []
    monkeypatch.setattr("orchestrator.time.sleep", lambda s: sleep_calls.append(s))
    monkeypatch.setattr("orchestrator.subprocess.run", lambda *a, **kw: _make_result(0))

    orchestrator.main()

    assert len(sleep_calls) == 3  # tiers 1→2, 2→3, 3→4 only


def test_every_tier_gets_the_same_daily_run_id_and_the_summary_is_sent_last(monkeypatch):
    seen_env = []
    monkeypatch.setattr(orchestrator, "COOLDOWN_SECONDS", 0)
    monkeypatch.setattr(orchestrator, "_send_tier_report", lambda _tier, _run_id: None)
    with patch("orchestrator.subprocess.run",
               side_effect=lambda cmd, env: seen_env.append(env["JDS_DAILY_RUN_ID"]) or
               type("R", (), {"returncode": 0})()), \
         patch("orchestrator.time.sleep"), \
         patch("orchestrator._send_day_summary") as report:
        orchestrator.main()
    assert len(seen_env) == 4 and len(set(seen_env)) == 1
    report.assert_called_once_with(seen_env[0])


def test_tier_report_is_sent_after_each_tier_and_before_its_cooldown(monkeypatch):
    events = []
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    with patch("orchestrator.load_dotenv"), \
         patch("orchestrator.telemetry.drain_buffers"), \
         patch("orchestrator.report_data.load_tier_settings", return_value={}), \
         patch("orchestrator.report_data.load_day", return_value=None), \
         patch("orchestrator._send_day_summary", lambda _run_id: None), \
         patch("orchestrator.subprocess.run", lambda *a, **kw: _make_result(0)), \
         patch("orchestrator.time.sleep", side_effect=lambda _: events.append("sleep")):
        # A report with no telemetry sends nothing; still, the call ordering is
        # what this asserts.
        monkeypatch.setattr(orchestrator, "_send_tier_report",
                            lambda tier, _run_id: events.append(f"report{tier}"))
        orchestrator.main()
    assert events == ["report1", "sleep", "report2", "sleep", "report3", "sleep", "report4"]


def test_send_report_messages_is_plain_text_one_send_per_message(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    with patch("orchestrator.send_message") as send:
        orchestrator._send_report_messages("Day summary", ["m1", "m2"])
    assert [c.args[0] for c in send.call_args_list] == ["m1", "m2"]
    assert all(c.kwargs["parse_mode"] is None for c in send.call_args_list)


def test_send_report_messages_log_line_reports_messages_sent(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    with patch("orchestrator.send_message", return_value=True) as send:
        orchestrator._send_report_messages("Day summary", ["m1", "m2", "m3", "m4"])
    assert send.call_count == 4
    assert "Day summary sent (4 messages)." in capsys.readouterr().out


def test_nine_messages_output_then_report_per_tier_then_summary(monkeypatch):
    from src import report_data as rd
    from tests.test_report_render import _report, SETTINGS
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    sent = []

    def fake_run(cmd, env):
        sent.append(f"OUTPUT tier {orchestrator.CONFIGS.index(cmd[-1]) + 1}")
        return _make_result(0)

    with patch("orchestrator.load_dotenv"), \
         patch("orchestrator.telemetry.drain_buffers"), \
         patch("orchestrator.report_data.load_tier_settings", return_value=SETTINGS), \
         patch("orchestrator.report_data.load_day", return_value=_report()), \
         patch("orchestrator.time.sleep"), \
         patch("orchestrator.subprocess.run", side_effect=fake_run), \
         patch("orchestrator.send_message",
               side_effect=lambda text, *a, **k: sent.append(text) or True):
        orchestrator.main()

    assert len(sent) == 9
    for i in range(4):
        assert sent[2 * i] == f"OUTPUT tier {i + 1}"
        report = sent[2 * i + 1]
        assert f"Tier {i + 1} · " in report
        assert "Legend:" in report
    assert "DAY SUMMARY" in sent[8]
    assert all("DAY SUMMARY" not in m for m in sent[:8])


def test_day_summary_is_plain_text_one_send_per_message(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    with patch("orchestrator.load_dotenv"), \
         patch("orchestrator.telemetry.drain_buffers"), \
         patch("orchestrator.report_data.load_day", return_value=None), \
         patch("orchestrator.report_render.render_summary_messages", return_value=["m1", "m2"]), \
         patch("orchestrator.send_message") as send:
        orchestrator._send_day_summary("day-1")
    assert [c.args[0] for c in send.call_args_list] == ["m1", "m2"]
    assert all(c.kwargs["parse_mode"] is None for c in send.call_args_list)


def test_tier_report_is_plain_text_one_send_per_message(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    with patch("orchestrator.load_dotenv"), \
         patch("orchestrator.telemetry.drain_buffers"), \
         patch("orchestrator.report_data.load_day", return_value=object()), \
         patch("orchestrator.report_render.render_tier_messages", return_value=["r1"]), \
         patch("orchestrator.send_message") as send:
        orchestrator._send_tier_report(1, "day-1")
    assert [c.args[0] for c in send.call_args_list] == ["r1"]
    assert all(c.kwargs["parse_mode"] is None for c in send.call_args_list)


def test_tier_report_without_telemetry_is_skipped(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    with patch("orchestrator.load_dotenv"), \
         patch("orchestrator.telemetry.drain_buffers"), \
         patch("orchestrator.report_data.load_day", return_value=None), \
         patch("orchestrator.send_message") as send:
        orchestrator._send_tier_report(1, "day-1")
    send.assert_not_called()
    assert "no telemetry was recorded" in capsys.readouterr().out


def test_day_summary_failure_never_raises(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    with patch("orchestrator.load_dotenv"), \
         patch("orchestrator.telemetry.drain_buffers", side_effect=RuntimeError("x")), \
         patch("orchestrator.report_data.load_day", side_effect=RuntimeError("neon down")), \
         patch("orchestrator.send_message", side_effect=RuntimeError("telegram down")):
        orchestrator._send_day_summary("day-1")


def test_tier_report_failure_never_raises(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    with patch("orchestrator.load_dotenv"), \
         patch("orchestrator.telemetry.drain_buffers", side_effect=RuntimeError("neon down")), \
         patch("orchestrator.report_data.load_day", side_effect=RuntimeError("neon down")), \
         patch("orchestrator.send_message", side_effect=RuntimeError("telegram down")):
        orchestrator._send_tier_report(2, "day-1")
    assert "Tier 2 report FAILED" in capsys.readouterr().out


def test_a_failed_middle_message_does_not_drop_the_rest(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    with patch("orchestrator.load_dotenv"), \
         patch("orchestrator.telemetry.drain_buffers"), \
         patch("orchestrator.report_data.load_day", return_value=None), \
         patch("orchestrator.report_render.render_summary_messages",
               return_value=["m1", "m2", "m3"]), \
         patch("orchestrator.send_message", side_effect=[True, RuntimeError("timeout"), False]):
        orchestrator._send_day_summary("day-1")
    out = capsys.readouterr().out
    assert "message(s) 2, 3 of 3 failed" in out
    assert "Day summary sent" not in out


def test_no_database_skips_the_summary_quietly(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with patch("orchestrator.load_dotenv"), patch("orchestrator.send_message") as send:
        orchestrator._send_day_summary("day-1")
    send.assert_not_called()
    assert "skipped" in capsys.readouterr().out


def test_no_database_skips_a_tier_report_quietly(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with patch("orchestrator.load_dotenv"), patch("orchestrator.send_message") as send:
        orchestrator._send_tier_report(1, "day-1")
    send.assert_not_called()
    assert "skipped" in capsys.readouterr().out
