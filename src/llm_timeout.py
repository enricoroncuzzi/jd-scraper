"""The one per-request time limit for every model call.

Handed to each OpenRouter `ChatOpenAI` client (scoring, remote verification, the
combined stage, tailoring) and to the Groq verification client through the
SDKs' own `timeout` argument, so a request that never answers raises the SDK's
APITimeoutError instead of blocking the service. That error is an openai/groq
APIConnectionError subclass, so each stage's existing retry ladder and fallback
already handle it, and telemetry records it as `timeout`.

What the limit is, and is not:
- It is per request, and it bounds silence between bytes (httpx's read timeout
  is per read), not total duration. A server that trickles keepalive bytes
  would not trip it.
- It is not a bound on a whole stuck call. Each ladder retries a timed-out
  request, and the scorer's client keeps the SDK default of 2 retries, so one
  scorer ladder attempt can take about 3 x 300 s = 15 minutes.

Why 300 s: the stall this stops lasted 3601 s, and the openai SDK's own
default is a 600 s read (`ChatOpenAI` without a timeout sets none at all). A
limit has to stay above real slow answers, though. Production telemetry, last 30
days, OpenRouter calls with outcome ok (latency_ms):
- scoring, 5640 calls: p50 33 s, p99 199 s, 65 over 180 s, 26 over 300 s, 7 over 600 s
- tailoring, 321 calls: p50 43 s, 8 over 180 s, 5 over 300 s, 3 over 600 s
- verification, 2464 calls: 22 over 180 s, 7 over 300 s, 4 over 600 s
So 180 s would have cut off real answers; 300 s keeps about 99.5% of successful
ones and caps a hung request at 5 minutes.

Groq already ships a 60 s read (and 5 s connect) as the SDK default and its
calls never exceed 4 s, so its client takes the smaller of the two and is never
loosened.

The helpers read the constant at call time, so a test can shorten it through
`llm_timeout.LLM_REQUEST_TIMEOUT_SECONDS`.
"""
import httpx

LLM_REQUEST_TIMEOUT_SECONDS = 300.0

_CONNECT_SECONDS = 5.0           # the SDKs' own default: a dead host fails fast
_GROQ_SDK_DEFAULT_SECONDS = 60.0  # groq.DEFAULT_TIMEOUT read/write/pool


def openrouter_timeout() -> httpx.Timeout:
    return httpx.Timeout(LLM_REQUEST_TIMEOUT_SECONDS, connect=_CONNECT_SECONDS)


def groq_timeout() -> httpx.Timeout:
    return httpx.Timeout(min(LLM_REQUEST_TIMEOUT_SECONDS, _GROQ_SDK_DEFAULT_SECONDS),
                         connect=_CONNECT_SECONDS)
