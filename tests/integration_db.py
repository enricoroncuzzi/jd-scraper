"""Guard for integration tests: they run only against a throwaway Neon branch.

Set JDS_TEST_DATABASE_URL to a branch created per plan Task 0. Never point it at
production - the fixture refuses when it equals DATABASE_URL.
"""
import os
import pytest

requires_test_db = pytest.mark.skipif(
    not os.environ.get("JDS_TEST_DATABASE_URL"),
    reason="JDS_TEST_DATABASE_URL not set (throwaway Neon branch, see run-observability plan Task 0)",
)


def _production_database_url() -> str | None:
    """The URL the process started with, not the one left after test fixtures.

    The autouse fixture deletes DATABASE_URL so a unit test cannot write
    through to production. Comparing against the live environment after that
    deletion would let a test URL that equals production through.
    """
    import sys
    for name in ("conftest", "tests.conftest"):
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "_PROCESS_DATABASE_URL"):
            return mod._PROCESS_DATABASE_URL
    return os.environ.get("DATABASE_URL")


def test_db_url() -> str:
    url = os.environ["JDS_TEST_DATABASE_URL"]
    production = _production_database_url()
    if production is not None and url == production:
        pytest.fail("refusing to run integration tests against the production DATABASE_URL")
    return url


test_db_url.__test__ = False
