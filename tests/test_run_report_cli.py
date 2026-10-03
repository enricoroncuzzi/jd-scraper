import runpy
import sys
from unittest.mock import patch
import pytest


def _run(argv, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run_report.py"] + argv)
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path("scripts/run_report.py", run_name="__main__")
    return exit_info.value.code


def test_missing_database_url_exits_1(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with patch("dotenv.load_dotenv"):
        assert _run(["day"], monkeypatch) == 1
    assert "DATABASE_URL" in capsys.readouterr().err


def test_day_prints_the_detail_view(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    with patch("dotenv.load_dotenv"), \
         patch("src.report_data.load_day", return_value=None) as load_day, \
         patch("src.report_render.render_day_detail", return_value="DETAIL"):
        assert _run(["day", "2026-10-05"], monkeypatch) == 0
    assert "DETAIL" in capsys.readouterr().out
    assert str(load_day.call_args.kwargs["day"]) == "2026-10-05"


def test_compare_passes_the_commit_and_prints(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    with patch("dotenv.load_dotenv"), \
         patch("src.report_data.load_daily_metrics", return_value=[]), \
         patch("src.report_render.render_compare", return_value="CMP") as render:
        assert _run(["compare", "abc1234"], monkeypatch) == 0
    assert render.call_args.args[0] == "abc1234" and "CMP" in capsys.readouterr().out
    assert render.call_args.kwargs["settings"]
