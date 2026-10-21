"""The daily fallback-model guard: which model actually answered, per stage.

Each stage pins a primary OpenRouter model (`_OPENROUTER_MODEL` in
`src/scorer.py`, `src/remote_verifier.py` and `src/tailor/generate.py`) and hands
OpenRouter up to three fallback models. OpenRouter answers with whichever model
it can reach and still returns a 200, so a broken primary is invisible in the
call's success: the only trace is `llm_calls.response_model`, which the sink
already records.

This module reads that trace. Per stage it computes today's (Europe/Rome) share
of answered OpenRouter calls that a fallback model answered rather than the
pinned primary, and the watchdog alerts Telegram once per stage per day when
that share crosses `share` with at least `min_calls` answered calls
(`config/llm_fallback.json`, `enabled` false switches the alert off). A call
that never answered is not a fallback answer: it is counted as unanswered and
left out of the share, because the stage's own failure surfaces elsewhere.

Nothing here changes a pin, a fallback list or a prompt: measuring only.
"""
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

from src.remote_verifier import _OPENROUTER_MODEL as _VERIFICATION_PRIMARY
from src.scorer import _OPENROUTER_MODEL as _SCORING_PRIMARY
from src.tailor.generate import _OPENROUTER_MODEL as _TAILORING_PRIMARY

CONFIG_PATH = "config/llm_fallback.json"
DEFAULT_SHARE = 0.20
DEFAULT_MIN_CALLS = 5
ROME = ZoneInfo("Europe/Rome")

# The stages that call OpenRouter through a primary plus a fallback chain, with
# the pin each one compares its answers against.
STAGE_PRIMARIES = {
    "scoring": _SCORING_PRIMARY,
    "verification": _VERIFICATION_PRIMARY,
    "tailoring": _TAILORING_PRIMARY,
}


@dataclass(frozen=True)
class GuardConfig:
    enabled: bool = True
    share: float = DEFAULT_SHARE
    min_calls: int = DEFAULT_MIN_CALLS


@dataclass(frozen=True)
class StageUsage:
    """One stage's OpenRouter calls today. `answers` holds the models that
    answered, most calls first (ties by name), never including "no answer"."""
    stage: str
    primary: str
    calls: int = 0
    unanswered: int = 0
    answers: tuple[tuple[str, int], ...] = field(default_factory=tuple)

    @property
    def answered(self) -> int:
        return self.calls - self.unanswered

    @property
    def primary_calls(self) -> int:
        return dict(self.answers).get(self.primary, 0)

    @property
    def primary_share(self) -> float:
        return self.primary_calls / self.answered if self.answered else 0.0

    @property
    def top_model(self) -> tuple[str, int] | None:
        return self.answers[0] if self.answers else None

    @property
    def fallbacks(self) -> tuple[tuple[str, int], ...]:
        """The answering models other than the primary, most calls first."""
        return tuple((model, count) for model, count in self.answers if model != self.primary)

    @property
    def fallback_calls(self) -> int:
        return sum(count for _model, count in self.fallbacks)


def load_guard_config(path: str = CONFIG_PATH) -> GuardConfig:
    """The shipped thresholds, or a safe default for a missing or malformed file.

    A file this cannot read leaves the guard on with the documented thresholds:
    silence is the failure mode this guard exists to end, so the unreadable case
    must not turn it off. One bad key falls back to that key's default alone.
    """
    try:
        with open(path) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return GuardConfig()
    if not isinstance(data, dict):
        return GuardConfig()
    enabled = data.get("enabled", True)
    share = data.get("share", DEFAULT_SHARE)
    min_calls = data.get("min_calls", DEFAULT_MIN_CALLS)
    if not isinstance(enabled, bool):
        enabled = True
    if isinstance(share, bool) or not isinstance(share, (int, float)) or not 0 < share <= 1:
        share = DEFAULT_SHARE
    if isinstance(min_calls, bool) or not isinstance(min_calls, int) or min_calls < 1:
        min_calls = DEFAULT_MIN_CALLS
    return GuardConfig(enabled=enabled, share=float(share), min_calls=min_calls)


def _zone_name(zone) -> str:
    return getattr(zone, "key", None) or str(zone)


def stage_usage(conn, stage: str, now: datetime, zone=ROME) -> StageUsage:
    """Today's OpenRouter calls for one stage, grouped by the model that answered."""
    primary = STAGE_PRIMARIES[stage]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT response_model, count(*) FROM llm_calls "
            "WHERE provider = 'openrouter' AND stage = %s "
            "AND (started_at AT TIME ZONE %s)::date = %s "
            "GROUP BY response_model",
            (stage, _zone_name(zone), now.astimezone(zone).date()),
        )
        rows = cur.fetchall()
    calls = sum(count for _model, count in rows)
    unanswered = sum(count for model, count in rows if model is None)
    answers = tuple(sorted(((model, count) for model, count in rows if model is not None),
                           key=lambda item: (-item[1], item[0])))
    return StageUsage(stage=stage, primary=primary, calls=calls, unanswered=unanswered, answers=answers)


def stage_usages(conn, now: datetime, zone=ROME) -> list[StageUsage]:
    return [stage_usage(conn, stage, now, zone) for stage in STAGE_PRIMARIES]


def over_threshold(usage: StageUsage, config: GuardConfig) -> bool:
    """Enough answered calls and a fallback share strictly above the threshold.

    Unanswered calls are left out of the share (and out of the floor): a model
    that never answered is not a fallback model answering.
    """
    return (usage.answered > 0
            and usage.answered >= config.min_calls
            and usage.fallback_calls / usage.answered > config.share)


def alert_text(usage: StageUsage, config: GuardConfig) -> str:
    models = ", ".join(f"{model} ({count})" for model, count in usage.fallbacks)
    share = round(100 * usage.fallback_calls / usage.answered)
    return (f"ALERT: OpenRouter fallback answering {usage.stage} calls: "
            f"{usage.fallback_calls} of {usage.answered} answered calls today ({share}%) "
            f"by {models}; primary {usage.primary} answered {usage.primary_calls}; "
            f"guard above {round(100 * config.share)}% from {config.min_calls} answered calls")


def _sent_day(cur, stage: str):
    cur.execute("SELECT value FROM kv_state WHERE key = %s", (f"llm_fallback:{stage}",))
    row = cur.fetchone()
    if row is None or not isinstance(row[0], dict):
        return None
    return row[0].get("day")


def run_guard(conn, config: GuardConfig, send, now: datetime, zone=ROME) -> list[str]:
    """Alert every stage whose fallback share crossed today, at most once per
    stage per Europe/Rome day. `send(text)` returns False on failure, which is
    not remembered, so the next check retries it. Returns the messages sent."""
    if not config.enabled:
        return []
    day = now.astimezone(zone).date()
    sent = []
    for usage in stage_usages(conn, now, zone):
        if not over_threshold(usage, config):
            continue
        with conn.cursor() as cur:
            if _sent_day(cur, usage.stage) == day.isoformat():
                continue
            text = alert_text(usage, config)
            if send(text) is False:
                continue
            cur.execute(
                "INSERT INTO kv_state (key, value, updated_at) VALUES (%s, %s::jsonb, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at",
                (f"llm_fallback:{usage.stage}", json.dumps({"day": day.isoformat(), "sent_at": now.isoformat()}), now))
        conn.commit()
        sent.append(text)
    return sent


def format_usage_lines(usages: list[StageUsage], config: GuardConfig) -> list[str]:
    """The operator view: per stage, today's calls, the primary's share and the
    top answering model."""
    guard = ("fallback guard alerts above "
             f"{round(100 * config.share)}% of a stage's answered calls from {config.min_calls}"
             if config.enabled else "fallback guard off")
    lines = [f"OpenRouter calls today (Rome), per stage ({guard}):"]
    for usage in usages:
        if usage.calls == 0:
            lines.append(f"  {usage.stage:<12} 0 calls")
            continue
        parts = [f"{usage.calls} calls"]
        if usage.unanswered:
            parts.append(f"{usage.unanswered} unanswered")
        parts.append(f"{round(100 * usage.primary_share)}% by the primary")
        if usage.top_model is not None:
            model, count = usage.top_model
            parts.append(f"top answering model {model} ({count})")
        if usage.fallbacks:
            model, count = usage.fallbacks[0]
            parts.append(f"fallback {model} ({count})")
        lines.append(f"  {usage.stage:<12} " + ", ".join(parts))
    return lines
