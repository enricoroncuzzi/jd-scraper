"""Connection helpers for the server-side job store (PostgreSQL on the VPS).

The job store is the working database of the collector, processor and delivery
services. It is distinct from Neon (DATABASE_URL), which only receives finished
jobs in batches (src/delivery/archive.py).
"""
import os
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg2


def store_url() -> str:
    url = os.environ.get("JOBSTORE_URL")
    if not url:
        raise RuntimeError("JOBSTORE_URL is not set (the server PostgreSQL connection string)")
    return url


def connect(url: str | None = None):
    """Open a connection with explicit transactions (autocommit off)."""
    return psycopg2.connect(url or store_url())


@contextmanager
def transaction(conn):
    """Commit when the block ends, roll back when it raises."""
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
