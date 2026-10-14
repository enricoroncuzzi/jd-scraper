"""Throwaway PostgreSQL for job-store tests. Never production.

Resolution order:
1. JDS_TEST_STORE_URL (CI service container, or a local Postgres you started).
2. An embedded server from the optional dev package `pixeltable-pgserver`
   (`pip install pixeltable-pgserver`), so no Docker is needed locally.
3. Otherwise store tests skip.

Each test gets its own schema, dropped afterwards, so tests never see each
other's rows.
"""
import os
import sys
import tempfile
import uuid
from pathlib import Path

import psycopg2
import pytest

_server = None


def _process_urls() -> set[str]:
    """Production connection strings the process started with (see conftest)."""
    urls = set()
    for name in ("conftest", "tests.conftest"):
        mod = sys.modules.get(name)
        if mod is not None:
            for attr in ("_PROCESS_JOBSTORE_URL", "_PROCESS_DATABASE_URL"):
                value = getattr(mod, attr, None)
                if value:
                    urls.add(value)
    return urls


def test_store_url() -> str | None:
    global _server
    url = os.environ.get("JDS_TEST_STORE_URL")
    if url:
        if url in _process_urls():
            pytest.fail("refusing to run store tests against a production connection string")
        return url
    try:
        import pixeltable_pgserver as pgserver
    except ImportError:
        return None
    if _server is None:
        _server = pgserver.get_server(Path(tempfile.gettempdir()) / "jds-test-pg", cleanup_mode="stop")
    return _server.get_uri()


test_store_url.__test__ = False


@pytest.fixture
def store_url_fixture():
    url = test_store_url()
    if url is None:
        pytest.skip("no test PostgreSQL: set JDS_TEST_STORE_URL or pip install pixeltable-pgserver")
    return url


@pytest.fixture
def store_conn(store_url_fixture):
    """A connection whose search_path is a fresh schema with the full job-store DDL."""
    from src.jobstore.schema import init_store

    schema = "t_" + uuid.uuid4().hex[:12]
    admin = psycopg2.connect(store_url_fixture)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema}")
    conn = psycopg2.connect(store_url_fixture, options=f"-c search_path={schema},public")
    try:
        init_store(conn)
        yield conn
    finally:
        conn.rollback()
        conn.close()
        with admin.cursor() as cur:
            cur.execute(f"DROP SCHEMA {schema} CASCADE")
        admin.close()


@pytest.fixture
def store_conn_factory(store_url_fixture):
    """Open extra connections onto the same schema as `store_conn`, for tests
    that need two sessions (advisory locks, concurrent writers)."""

    def make(conn):
        with conn.cursor() as cur:
            cur.execute("SHOW search_path")
            path = cur.fetchone()[0]
        return psycopg2.connect(store_url_fixture, options=f"-c search_path={path}")

    return make
