import psycopg2
from tests.integration_db import requires_test_db, test_db_url
from src.storage import init_db


def _columns(cur, table):
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,))
    return {row[0] for row in cur.fetchall()}


@requires_test_db
def test_init_db_evolves_a_production_shaped_database_and_is_rerunnable():
    url = test_db_url()
    init_db(url)
    init_db(url)  # must be idempotent against its own output
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            assert {"run_uuid", "daily_run_id", "status", "telemetry_ok",
                    "verification_rejected"} <= _columns(cur, "runs")
            assert {"stop_reason", "run_uuid"} <= _columns(cur, "run_queries")
            assert {"response_model", "prompt_version", "outcome"} <= _columns(cur, "llm_calls")
            cur.execute("SELECT COUNT(*) FROM runs")
            assert cur.fetchone()[0] > 0  # the branch carries production history; it survived
    finally:
        conn.close()
