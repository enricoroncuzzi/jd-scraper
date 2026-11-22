"""A model call that never answers is abandoned, not waited on.

On 2026-11-11 one OpenRouter call made by jds-process never returned and the
service sat silent for an hour. These tests stand up a local server that
accepts a request and then says nothing, point each real client at it with the
time limit shortened, and check the call gives up quickly and lands in the
existing retry path. No network beyond 127.0.0.1, no keys, no database.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import groq
import httpx
import openai
import pytest

from src import llm_timeout, scorer, telemetry
from src import remote_verifier as rv
from src.models import JobOffer
from src.processor import combined
from src.tailor import generate

SHORT_LIMIT = 0.5   # seconds, replaces the production limit in these tests
DEADLINE = 10.0     # a call still running after this long is a hung call


class _FakeModelServer:
    """Stalls the first `stall` POSTs until released, then answers like a model."""

    def __init__(self, stall: int):
        self.stall = stall
        self.requests = 0
        self.release = threading.Event()
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests += 1
                if outer.requests <= outer.stall:
                    outer.release.wait(60)
                    return
                raw = json.dumps(outer.answer(body)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    @staticmethod
    def answer(body: dict) -> dict:
        if "tools" in body:  # a forced tool call, the shape every chain asks for
            call = {"id": "c1", "type": "function", "function": {
                "name": body["tools"][0]["function"]["name"],
                "arguments": json.dumps({"offers": [
                    {"id": 0, "score": 9, "comment": "fit", "summary": "role"}]})}}
            message = {"role": "assistant", "content": "", "tool_calls": [call]}
        else:
            message = {"role": "assistant", "content": json.dumps({"offers": []})}
        return {"id": "g", "object": "chat.completion", "created": 0, "model": body["model"],
                "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_server(monkeypatch):
    monkeypatch.setattr(llm_timeout, "LLM_REQUEST_TIMEOUT_SECONDS", SHORT_LIMIT)
    servers = []

    def make(stall: int) -> _FakeModelServer:
        servers.append(_FakeModelServer(stall))
        return servers[-1]

    yield make
    for server in servers:
        server.close()


def _run_with_deadline(fn):
    """Run fn in a thread; fail the test if it is still blocked at the deadline."""
    outcome: dict = {}

    def target():
        try:
            outcome["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - the test inspects it
            outcome["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(DEADLINE)
    if thread.is_alive():
        pytest.fail(f"the model call was still blocked after {DEADLINE}s: no time limit")
    return outcome


def _chain_payload(chain) -> dict:
    return {name: "x" for name in chain.first.input_variables}


def _openrouter_sites(monkeypatch, url: str):
    monkeypatch.setattr(scorer, "_OPENROUTER_BASE_URL", url + "/api/v1")
    monkeypatch.setattr(rv, "_OPENROUTER_BASE_URL", url + "/api/v1")
    monkeypatch.setattr(combined, "_OPENROUTER_BASE_URL", url + "/api/v1")
    monkeypatch.setattr(generate, "_OPENROUTER_BASE_URL", url + "/api/v1")
    return {
        "scorer": scorer._build_chain,
        "verifier": rv._openrouter_chain,
        "combined": combined.build_chain,
        "tailor": generate._build_chain,
    }


# Requests sent before the call gives up: the scorer's ChatOpenAI keeps the SDK
# default of 2 retries, the other three set max_retries=0.
@pytest.mark.parametrize("site, requests", [
    ("scorer", 3), ("verifier", 1), ("combined", 1), ("tailor", 1)])
def test_a_stalled_openrouter_call_is_abandoned_as_a_timeout(site, requests, fake_server, monkeypatch):
    server = fake_server(stall=99)
    chain = _openrouter_sites(monkeypatch, server.url)[site]("key")

    outcome = _run_with_deadline(lambda: chain.invoke(_chain_payload(chain)))

    assert isinstance(outcome.get("error"), openai.APITimeoutError)
    assert telemetry.classify_llm_error(outcome["error"]) == "timeout"
    assert server.requests == requests


def test_a_stalled_groq_call_is_abandoned_as_a_timeout(fake_server, monkeypatch):
    server = fake_server(stall=99)
    monkeypatch.setenv("GROQ_BASE_URL", server.url)
    client = rv._client("key")

    outcome = _run_with_deadline(lambda: client.chat.completions.create(
        model="m", messages=[{"role": "user", "content": "x"}]))

    assert isinstance(outcome.get("error"), groq.APITimeoutError)
    assert telemetry.classify_llm_error(outcome["error"]) == "timeout"
    assert server.requests == 1


def test_the_scorer_retries_a_stalled_call_with_its_existing_backoff(fake_server, monkeypatch):
    # The scorer's ChatOpenAI keeps the SDK's default of 2 retries, so its own
    # ladder only sees the third stalled request fail.
    server = fake_server(stall=3)
    monkeypatch.setattr(scorer, "_OPENROUTER_BASE_URL", server.url + "/api/v1")
    waits = []
    monkeypatch.setattr(scorer.time, "sleep", waits.append)
    offers = [JobOffer(id=0, title="AI Engineer", company="Acme", location="Milan",
                       link="https://li.com/1", description="LLM pipeline")]

    outcome = _run_with_deadline(lambda: scorer._invoke_batch(
        scorer._build_chain("key"), offers, "profile", ["LLM"], [], scorer._TokenCounter()))

    assert [item.score for item in outcome["value"]] == [9]
    assert waits[-1] == 10  # the ladder's own first-attempt backoff, no new ladder
    assert server.requests == 4


def test_the_verifier_retries_a_stalled_groq_call_with_its_existing_backoff(fake_server, monkeypatch):
    server = fake_server(stall=1)
    monkeypatch.setenv("GROQ_BASE_URL", server.url)
    waits = []
    monkeypatch.setattr(rv.time, "sleep", waits.append)
    monkeypatch.setattr(rv, "_resolve_items", lambda items, batch, req: {"resolved": True})
    monkeypatch.setattr(rv._AnswerOutput, "model_validate_json",
                        classmethod(lambda cls, raw: cls(offers=[])))
    offers = [JobOffer(id=0, title="AI Engineer", company="Acme", location="Milan",
                       link="https://li.com/1", description="LLM pipeline")]

    outcome = _run_with_deadline(lambda: rv._verify_batch(rv._client("key"), offers, False))

    assert outcome["value"][0] == {"resolved": True}
    assert waits == [5]  # the existing first-attempt backoff, no new ladder
    assert server.requests == 2


def test_the_production_limit_is_a_few_minutes_not_the_sdk_default():
    # openai's own default is a 600 s read (the 2026-10-21 ten-minute hang); the
    # limit must stay under that, and at or above 300 s, which keeps about 99.5%
    # of the successful answers seen in production telemetry (180 s cut real ones).
    assert 300 <= llm_timeout.LLM_REQUEST_TIMEOUT_SECONDS < 600


def test_the_production_groq_client_is_no_looser_than_groqs_own_default():
    # Groq already ships Timeout(connect=5, read=60, write=60, pool=60); the one
    # shared limit must never loosen it.
    timeout = rv._client("key").timeout
    default = groq.DEFAULT_TIMEOUT
    assert isinstance(timeout, httpx.Timeout)
    for field in ("connect", "read", "write", "pool"):
        assert getattr(timeout, field) <= getattr(default, field), field


@pytest.mark.parametrize("site", ["scorer", "verifier", "combined", "tailor"])
def test_the_production_openrouter_clients_use_the_limit_and_fail_fast_on_connect(site, monkeypatch):
    chain = _openrouter_sites(monkeypatch, "http://127.0.0.1:9")[site]("key")
    llm = next(step for step in _walk(chain) if hasattr(step, "root_client"))
    timeout = llm.root_client.timeout
    assert timeout.read == llm_timeout.LLM_REQUEST_TIMEOUT_SECONDS
    assert timeout.connect == 5.0


def _walk(runnable):
    seen, stack = [], [runnable]
    while stack:
        node = stack.pop()
        seen.append(node)
        for attr in ("steps", "first", "middle", "last", "bound"):
            child = getattr(node, attr, None)
            if isinstance(child, list):
                stack.extend(child)
            elif child is not None:
                stack.append(child)
    return seen
