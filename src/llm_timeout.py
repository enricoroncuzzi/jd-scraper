"""The one per-request time limit for every model call.

Passed as the client libraries' own `timeout` to each OpenRouter `ChatOpenAI`
client (scoring, remote verification, the combined stage, tailoring) and to the
Groq verification client, so a call that never answers raises the SDK's
APITimeoutError instead of blocking the service. That error is an
openai/groq APIConnectionError subclass, so each stage's existing retry ladder
and fallback already handle it, and telemetry records it as `timeout`.

Why 180 s: the openai SDK's default is a 600 s read (the ten-minute stall of
2026-10-21) and the stall of 2026-11-11 lasted an hour. A batch is 5 to 8 offers
and the answer is a few hundred tokens of structured output, so even a slow free
model answers in well under a minute; 180 s leaves headroom for a slow one while
bounding a stuck call to three minutes. Read it as `llm_timeout.LLM_REQUEST_TIMEOUT_SECONDS`
(not `from ... import`) so a test can shorten it.
"""

LLM_REQUEST_TIMEOUT_SECONDS = 180.0
