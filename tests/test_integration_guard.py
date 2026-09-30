"""The integration-test guard must refuse a test URL that is production.

The autouse fixture deletes DATABASE_URL before a test body runs. The guard
has to remember the URL the process started with, and the suite must not
load the real .env while collecting tests.
"""
import os
import sys
from unittest.mock import patch

import pytest

from tests import integration_db


def _pin_production_url(monkeypatch, url):
    for name in ("conftest", "tests.conftest"):
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "_PROCESS_DATABASE_URL"):
            monkeypatch.setattr(mod, "_PROCESS_DATABASE_URL", url)


def test_equal_urls_never_connect(monkeypatch):
    monkeypatch.setenv("JDS_TEST_DATABASE_URL", "postgresql://same/db")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _pin_production_url(monkeypatch, "postgresql://same/db")
    with patch("psycopg2.connect") as connect:
        with pytest.raises(pytest.fail.Exception, match="production"):
            integration_db.test_db_url()
        connect.assert_not_called()


def test_a_distinct_test_url_is_accepted(monkeypatch):
    monkeypatch.setenv("JDS_TEST_DATABASE_URL", "postgresql://branch/db")
    _pin_production_url(monkeypatch, "postgresql://production/db")
    assert integration_db.test_db_url() == "postgresql://branch/db"


def test_dotenv_is_disabled_for_the_suite(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("JDS_DOTENV_PROBE=loaded\nDATABASE_URL=postgresql://from-file/db\n")
    monkeypatch.delenv("JDS_DOTENV_PROBE", raising=False)
    import dotenv
    assert dotenv.load_dotenv() is False
    assert os.environ.get("JDS_DOTENV_PROBE") is None
    assert os.environ.get("DATABASE_URL") != "postgresql://from-file/db"
