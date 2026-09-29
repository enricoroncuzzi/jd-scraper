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


def test_db_url() -> str:
    url = os.environ["JDS_TEST_DATABASE_URL"]
    if url == os.environ.get("DATABASE_URL"):
        pytest.fail("refusing to run integration tests against the production DATABASE_URL")
    return url
