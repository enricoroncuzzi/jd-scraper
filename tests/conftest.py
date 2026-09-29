import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _block_production_database_url(monkeypatch):
    """tailor.py calls load_dotenv() at import, which can put the real
    DATABASE_URL into the environment before any test runs. llm_call writes
    through to that URL when there is no telemetry session. Drop it for every
    test; a test that needs one sets it afterwards with monkeypatch.
    JDS_TEST_DATABASE_URL is left in place."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
