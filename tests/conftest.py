import sys
import os

import dotenv
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Snapshot before any fixture deletes it, and before test modules import
# tailor.py. The suite must not read the real .env: load_dotenv at import
# used to put the production DATABASE_URL into the environment, and the
# autouse fixture below then deleted it before the integration guard compared.
_PROCESS_DATABASE_URL = os.environ.get("DATABASE_URL")


def _tests_do_not_read_dotenv(*args, **kwargs):
    return False


dotenv.load_dotenv = _tests_do_not_read_dotenv


@pytest.fixture(autouse=True)
def _block_production_database_url(monkeypatch):
    """llm_call writes through to DATABASE_URL when there is no telemetry
    session. Drop it for every test; a test that needs one sets it afterwards
    with monkeypatch. JDS_TEST_DATABASE_URL is left in place. The production
    value, if the process was started with one, is kept on
    tests.conftest._PROCESS_DATABASE_URL for the integration guard."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
