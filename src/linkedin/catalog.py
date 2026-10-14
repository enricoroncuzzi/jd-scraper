"""The LinkedIn slice catalog: one slice per (role, area), built from the tier configs.

A slice is one role in one area (a country name LinkedIn's `location` accepts).
Tier 3 has one slice per EU/EEA country except Italy, instead of one "Europe"
search, so no single query comes near LinkedIn's ~900-result ceiling.
"""
import json
from dataclasses import dataclass
from datetime import datetime

from src.jobstore.db import utcnow

SOURCE = "linkedin"
TIER_CONFIG_PATHS = [f"config/config_tier{n}.json" for n in (1, 2, 3, 4)]
COLLECTOR_CONFIG_PATH = "config/collector.json"


@dataclass(frozen=True)
class CollectorConfig:
    due_hours: dict[int, int]
    max_pages_per_slice: int = 90
    known_pages_to_stop: int = 3
    window_margin_hours: int = 2
    legacy_lock_path: str | None = None


@dataclass(frozen=True)
class SliceSpec:
    source: str
    role: str
    area: str
    tier: int
    base_interval_seconds: int


def load_collector_config(path: str = COLLECTOR_CONFIG_PATH) -> CollectorConfig:
    with open(path) as handle:
        data = json.load(handle)
    return CollectorConfig(
        due_hours={int(tier): int(hours) for tier, hours in data["due_hours"].items()},
        max_pages_per_slice=int(data.get("max_pages_per_slice", 90)),
        known_pages_to_stop=int(data.get("known_pages_to_stop", 3)),
        window_margin_hours=int(data.get("window_margin_hours", 2)),
        legacy_lock_path=data.get("legacy_lock_path"),
    )


def load_tier_configs(paths: list[str] = TIER_CONFIG_PATHS) -> dict[int, dict]:
    """Raw tier JSON by tier number. Deliberately not load_config(): that needs
    every secret in the environment, and slices need none of them."""
    configs = {}
    for path in paths:
        with open(path) as handle:
            data = json.load(handle)
        configs[int(data["tier"])] = data
    return configs


def _areas(search: dict) -> list[str]:
    return list(search.get("countries") or search.get("allowed_countries") or [search["location"]])


def build_slices(tier_configs: dict[int, dict], due_hours: dict[int, int]) -> list[SliceSpec]:
    specs: list[SliceSpec] = []
    seen: set[tuple[str, str]] = set()
    for tier in sorted(tier_configs):
        search = tier_configs[tier]["search"]
        for area in _areas(search):
            for role in search["roles"]:
                if (role, area) in seen:
                    continue
                seen.add((role, area))
                specs.append(SliceSpec(SOURCE, role, area, tier, due_hours[tier] * 3600))
    return specs


def sync_slices(conn, specs: list[SliceSpec], now: datetime | None = None,
                source: str = SOURCE) -> dict[str, int]:
    """Make the `slices` table match `specs` for one source: add new ones (due at
    `now`), update tier and base cadence, and deactivate slices no longer
    configured. The caller commits."""
    now = now or utcnow()
    counts = {"added": 0, "updated": 0, "deactivated": 0}
    wanted = {(s.role, s.area): s for s in specs}
    with conn.cursor() as cur:
        cur.execute("SELECT id, role, area, tier, base_interval_seconds, active FROM slices "
                    "WHERE source = %s", (source,))
        existing = {(role, area): (sid, tier, base, active)
                    for sid, role, area, tier, base, active in cur.fetchall()}
        for key, spec in wanted.items():
            if key not in existing:
                cur.execute(
                    "INSERT INTO slices (source, role, area, tier, base_interval_seconds, "
                    "interval_seconds, due_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (source, spec.role, spec.area, spec.tier, spec.base_interval_seconds,
                     spec.base_interval_seconds, now))
                counts["added"] += 1
                continue
            sid, tier, base, active = existing[key]
            if (tier, base, active) != (spec.tier, spec.base_interval_seconds, True):
                cur.execute(
                    "UPDATE slices SET tier = %s, base_interval_seconds = %s, active = TRUE, "
                    "interval_seconds = LEAST(interval_seconds, %s) WHERE id = %s",
                    (spec.tier, spec.base_interval_seconds, spec.base_interval_seconds, sid))
                counts["updated"] += 1
        for key, (sid, _tier, _base, active) in existing.items():
            if key not in wanted and active:
                cur.execute("UPDATE slices SET active = FALSE WHERE id = %s", (sid,))
                counts["deactivated"] += 1
    return counts
