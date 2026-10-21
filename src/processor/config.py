"""Processor settings: global knobs from config/processor.json, per-tier policy
from the existing config/config_tier{1..4}.json (profile, keywords, remote rules,
threshold), so the scoring behavior has one source of truth."""
import json
from dataclasses import dataclass

from src.processor.fetch_order import (
    DEFAULT_FETCH_ORDER, FetchOrder, load_fetch_order,
)

PROCESSOR_CONFIG_PATH = "config/processor.json"
TIER_CONFIG_PATHS = [f"config/config_tier{n}.json" for n in (1, 2, 3, 4)]


@dataclass(frozen=True)
class TierPolicy:
    tier: int
    remote_check_enabled: bool
    require_italy_eligibility: bool
    threshold: int
    exclude_keywords: tuple[str, ...]
    priority_keywords: tuple[str, ...]
    candidate_profile: str
    greeting: str
    title_prefilter: bool = False
    autoapply_enabled: bool = False
    autoapply_dry_run: bool = True

    @property
    def scoring_key(self) -> tuple:
        """Tiers with the same key can share one scoring batch."""
        return (self.candidate_profile, self.priority_keywords, self.exclude_keywords)


@dataclass(frozen=True)
class ProcessorConfig:
    policies: dict[int, TierPolicy]
    remote_check_batch_size: int = 8
    scoring_batch_size: int = 5
    flush_after_minutes: int = 30
    descriptions_per_tick: int = 20
    description_seconds_per_tick: int = 120
    description_deadline_hours: int = 24
    description_urgent_after_hours: int = 60
    cheap_checks_per_tick: int = 200
    refusal_expiry_days: int = 7
    posting_max_age_days: int = 30
    backoff_first_minutes: int = 30
    backoff_cap_hours: int = 12
    llm_stop_at_requests: int = 950
    # The source's structured work mode (freehire, Himalayas) is a shortcut only
    # for hybrid/on-site: the scout measured 99.8% agreement on 420 decided jobs.
    structured_remote_enabled: bool = True
    # The deterministic phrase rules (src/remote_rules.py) that run before the
    # model. See that module for what they will and will not decide.
    remote_rules_enabled: bool = True
    # Occasional presence (onboarding, team events, offsites, sprints, meetups)
    # is not a hybrid phrase. Off restores the pre-2026-10-21 rule behaviour.
    occasional_presence_guard: bool = True
    # Technology-only "hybrid" wording (hybrid cloud, hybrid environments) is not
    # a work mode. Off restores the pre-2026-10-21 rule behaviour.
    cloud_hybrid_guard: bool = True
    # A source "remote"/"remote_worldwide" hint plus a silent description is
    # advised, not not_stated: the source's own field is evidence.
    remote_hint_advice: bool = True
    merged_call_enabled: bool = False
    # Which waiting description is worth fetching next (src/processor/fetch_order.py).
    fetch_order: FetchOrder = DEFAULT_FETCH_ORDER


def load_policies(paths: list[str], title_prefilter: dict[int, bool]) -> dict[int, TierPolicy]:
    policies = {}
    for path in paths:
        with open(path) as handle:
            data = json.load(handle)
        tier = int(data["tier"])
        remote = data.get("remote_check", {})
        scoring = data["scoring"]
        policies[tier] = TierPolicy(
            tier=tier,
            remote_check_enabled=bool(remote.get("enabled", False)),
            require_italy_eligibility=bool(remote.get("require_italy_eligibility", True)),
            threshold=int(scoring["threshold"]),
            exclude_keywords=tuple(scoring["exclude_keywords"]),
            priority_keywords=tuple(scoring["priority_keywords"]),
            candidate_profile=scoring["candidate_profile"],
            greeting=data["telegram"]["greeting"],
            title_prefilter=bool(title_prefilter.get(tier, False)),
            autoapply_enabled=bool(data.get("autoapply", {}).get("enabled", False)),
            autoapply_dry_run=bool(data.get("autoapply", {}).get("dry_run", True)),
        )
    return policies


def _require_hours(data: dict, key: str, default: int, where: str = "processor") -> int:
    """A whole number of hours >= 1: a non-positive or non-integer deadline would
    silently disable (or invert) the guard that reads it."""
    value = data.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{where}.{key} must be an integer >= 1, got {value!r}")
    return value


def load_processor_config(path: str = PROCESSOR_CONFIG_PATH,
                          tier_paths: list[str] = TIER_CONFIG_PATHS) -> ProcessorConfig:
    with open(path) as handle:
        data = json.load(handle)
    prefilter = {int(tier): bool(flag) for tier, flag in data.get("title_prefilter", {}).items()}
    return ProcessorConfig(
        policies=load_policies(tier_paths, prefilter),
        remote_check_batch_size=int(data["remote_check_batch_size"]),
        scoring_batch_size=int(data["scoring_batch_size"]),
        flush_after_minutes=int(data["flush_after_minutes"]),
        descriptions_per_tick=int(data["descriptions_per_tick"]),
        description_seconds_per_tick=int(data.get("description_seconds_per_tick", 120)),
        description_deadline_hours=_require_hours(data, "description_deadline_hours", 24),
        description_urgent_after_hours=_require_hours(data, "description_urgent_after_hours", 60),
        cheap_checks_per_tick=int(data["cheap_checks_per_tick"]),
        refusal_expiry_days=int(data["refusal_expiry_days"]),
        posting_max_age_days=int(data["posting_max_age_days"]),
        backoff_first_minutes=int(data["backoff_first_minutes"]),
        backoff_cap_hours=int(data["backoff_cap_hours"]),
        llm_stop_at_requests=int(data["llm_stop_at_requests"]),
        structured_remote_enabled=bool(
            data.get("structured_remote", {}).get("enabled", True)),
        remote_rules_enabled=bool(data.get("remote_rules", {}).get("enabled", True)),
        occasional_presence_guard=bool(
            data.get("remote_rules", {}).get("occasional_presence", True)),
        cloud_hybrid_guard=bool(data.get("remote_rules", {}).get("cloud_hybrid", True)),
        remote_hint_advice=bool(
            data.get("remote_hint", {}).get("advised_when_silent", True)),
        merged_call_enabled=bool(data.get("merged_call", {}).get("enabled", False)),
        fetch_order=load_fetch_order(data.get("description_order")),
    )
