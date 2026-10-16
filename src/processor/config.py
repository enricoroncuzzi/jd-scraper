"""Processor settings: global knobs from config/processor.json, per-tier policy
from the existing config/config_tier{1..4}.json (profile, keywords, remote rules,
threshold), so the scoring behavior has one source of truth."""
import json
from dataclasses import dataclass

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
    cheap_checks_per_tick: int = 200
    refusal_expiry_days: int = 7
    posting_max_age_days: int = 30
    backoff_first_minutes: int = 30
    backoff_cap_hours: int = 12
    llm_stop_at_requests: int = 950
    structured_remote_enabled: bool = False
    merged_call_enabled: bool = False


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
        description_deadline_hours=int(data.get("description_deadline_hours", 24)),
        cheap_checks_per_tick=int(data["cheap_checks_per_tick"]),
        refusal_expiry_days=int(data["refusal_expiry_days"]),
        posting_max_age_days=int(data["posting_max_age_days"]),
        backoff_first_minutes=int(data["backoff_first_minutes"]),
        backoff_cap_hours=int(data["backoff_cap_hours"]),
        llm_stop_at_requests=int(data["llm_stop_at_requests"]),
        structured_remote_enabled=bool(data.get("structured_remote", {}).get("enabled", False)),
        merged_call_enabled=bool(data.get("merged_call", {}).get("enabled", False)),
    )
