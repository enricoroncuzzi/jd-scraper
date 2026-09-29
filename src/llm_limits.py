"""Provider daily limits, kept in one place (config/llm_limits.json).

Limits are perishable - providers change them without notice - so every value
is read off the live provider when it is set, and an unverifiable one is null.
Nothing here may raise: a missing or malformed file means "no limits known",
and callers fall back to their own safe defaults.
"""
import json
from dataclasses import dataclass

DEFAULT_PATH = "config/llm_limits.json"
_UNITS = ("tokens", "requests")


@dataclass(frozen=True)
class Limit:
    provider: str
    model: str
    unit: str
    per_day: int | None


def _parse(entry) -> Limit | None:
    if not isinstance(entry, dict):
        return None
    provider, model, unit = entry.get("provider"), entry.get("model"), entry.get("unit")
    per_day = entry.get("per_day")
    if not isinstance(provider, str) or not isinstance(model, str) or unit not in _UNITS:
        return None
    if per_day is not None and (isinstance(per_day, bool) or not isinstance(per_day, int) or per_day <= 0):
        return None
    return Limit(provider, model, unit, per_day)


def load_limits(path: str = DEFAULT_PATH) -> list[Limit]:
    try:
        with open(path) as f:
            entries = json.load(f).get("limits")
    except Exception:
        return []
    if not isinstance(entries, list):
        return []
    return [limit for limit in (_parse(e) for e in entries) if limit is not None]


def limit_for(provider: str, model: str, limits: list[Limit] | None = None) -> Limit | None:
    limits = load_limits() if limits is None else limits
    exact = [l for l in limits if l.provider == provider and l.model == model]
    if exact:
        return exact[0]
    wildcard = [l for l in limits if l.provider == provider and l.model == "*"]
    return wildcard[0] if wildcard else None
