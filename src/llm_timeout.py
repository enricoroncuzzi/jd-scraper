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
  scorer ladder attempt can take about 3 x 180 s = 9 minutes.

Why 180 s: the openai SDK's default is a 600 s read (the ten-minute stall of
2026-10-21; `ChatOpenAI` without a timeout sets none at all) and the stall of
2026-11-11 lasted an hour. A batch is 5 to 8 offers and the answer is a few
hundred tokens of structured output, so even a slow free model answers in well
under a minute; 180 s leaves headroom for a slow one.

Groq already ships a 60 s read (and 5 s connect) as the SDK default, so its
client takes the smaller of the two and is never loosened.

The helpers read the constant at call time, so a test can shorten it through
`llm_timeout.LLM_REQUEST_TIMEOUT_SECONDS`.
"""
import httpx

LLM_REQUEST_TIMEOUT_SECONDS = 180.0

_CONNECT_SECONDS = 5.0           # the SDKs' own default: a dead host fails fast
_GROQ_SDK_DEFAULT_SECONDS = 60.0  # groq.DEFAULT_TIMEOUT read/write/pool


def openrouter_timeout() -> httpx.Timeout:
    return httpx.Timeout(LLM_REQUEST_TIMEOUT_SECONDS, connect=_CONNECT_SECONDS)


def groq_timeout() -> httpx.Timeout:
    return httpx.Timeout(min(LLM_REQUEST_TIMEOUT_SECONDS, _GROQ_SDK_DEFAULT_SECONDS),
                         connect=_CONNECT_SECONDS)
