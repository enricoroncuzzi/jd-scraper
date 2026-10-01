def apply_transaction_timeouts(conn):
    """Bound statements and lock waits for the transaction already in progress.

    Neon pooled endpoints reject these settings as startup parameters. SET LOCAL
    is transaction-scoped, so it is safe with PgBouncer transaction pooling.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '15000ms'")
            cur.execute("SET LOCAL lock_timeout = '5000ms'")
    except Exception:
        conn.close()
        raise
    return conn
