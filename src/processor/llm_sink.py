"""Record every language-model call in the server store.

Installed with telemetry.set_llm_sink(). Each call attempt (retries included) is
one provider request, so each one adds a row to llm_calls and one request to the
day's llm_budget. It must never break a pipeline stage: failures are printed and
the connection is reopened on the next call.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime

from src.jobstore import budget

_batch_job_ids: ContextVar[tuple[str, ...]] = ContextVar("batch_job_ids", default=())


@contextmanager
def batch_context(job_ids):
    """Tie every LLM call made inside the block to these jobs (llm_call_jobs)."""
    token = _batch_job_ids.set(tuple(job_ids))
    try:
        yield
    finally:
        _batch_job_ids.reset(token)


class StoreLlmSink:
    def __init__(self, connect_fn, service: str):
        self._connect = connect_fn
        self.service = service
        self._conn = None

    def _connection(self):
        if self._conn is None or self._conn.closed:
            self._conn = self._connect()
            self._conn.autocommit = True
        return self._conn

    def __call__(self, data: dict) -> None:
        try:
            conn = self._connection()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO llm_calls (id, service, stage, provider, request_model,
                        response_model, input_tokens, output_tokens, latency_ms, batch_size,
                        attempt, outcome, error, prompt_version, started_at)
                    VALUES (%(id)s, %(service)s, %(stage)s, %(provider)s, %(request_model)s,
                        %(response_model)s, %(input_tokens)s, %(output_tokens)s, %(latency_ms)s,
                        %(batch_size)s, %(attempt)s, %(outcome)s, %(error)s, %(prompt_version)s,
                        %(started_at)s)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    {**data, "service": self.service},
                )
                for job_id in _batch_job_ids.get():
                    cur.execute(
                        "INSERT INTO llm_call_jobs (call_id, job_id) VALUES (%s, %s) "
                        "ON CONFLICT DO NOTHING", (data["id"], job_id))
            budget.add_usage(
                conn, data["provider"], requests=1,
                input_tokens=data.get("input_tokens") or 0,
                output_tokens=data.get("output_tokens") or 0,
                now=datetime.fromisoformat(data["started_at"]),
            )
        except Exception as exc:  # noqa: BLE001 - telemetry must not break a stage
            print(f"[llm-sink] could not record a call ({type(exc).__name__}: {exc})")
            self._conn = None
