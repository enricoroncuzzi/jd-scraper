"""The LinkedIn slice catalog: one slice per (role, area), built from the tier configs.

A slice is one role in one area (a country name LinkedIn's `location` accepts).
Tier 3 has one slice per EU/EEA country except Italy, instead of one "Europe"
search, so no single query comes near LinkedIn's ~900-result ceiling.
"""
import json
from dataclasses import dataclass, field
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
    area_due_hours: dict[str, int] = field(default_factory=dict)
    area_max_pages: dict[str, int] = field(default_factory=dict)
    refused_pages_to_defer: int = 3
    min_interval_hours: int = 12
    max_interval_hours: int = 48
    cadence_tolerance_hours: int = 6
    skip_slices: frozenset = frozenset()  # (role, area) pairs that are not walked

    def max_pages_for(self, area: str) -> int:
        """The walk page cap for one area: the area cap when set, else the default."""
        return self.area_max_pages.get(area, self.max_pages_per_slice)


@dataclass(frozen=True)
class SliceSpec:
    source: str
    role: str
    area: str
    tier: int
    base_interval_seconds: int


def _require_int(data: dict, key: str, where: str, *, minimum: int = 0) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{where}.{key} must be an integer >= {minimum}, got {value!r}")
    return value


def load_collector_config(path: str = COLLECTOR_CONFIG_PATH) -> CollectorConfig:
    with open(path) as handle:
        data = json.load(handle)
    due_hours = {int(tier): int(hours) for tier, hours in data["due_hours"].items()}
    area_due_hours = {str(area): int(hours) for area, hours in data.get("area_due_hours", {}).items()}
    if any(hours < 1 for hours in area_due_hours.values()):
        raise ValueError("area_due_hours must be at least 1 hour")
    area_max_pages = {str(area): int(pages) for area, pages in data.get("area_max_pages", {}).items()}
    if any(pages < 1 for pages in area_max_pages.values()):
        raise ValueError("area_max_pages must be at least 1 page")
    refused_pages_to_defer = data.get("refused_pages_to_defer", 3)
    if not isinstance(refused_pages_to_defer, int) or isinstance(refused_pages_to_defer, bool) \
            or refused_pages_to_defer < 1:
        raise ValueError("refused_pages_to_defer must be an integer >= 1")
    min_interval_hours = _require_int(data, "min_interval_hours", "collector", minimum=1)
    max_interval_hours = _require_int(data, "max_interval_hours", "collector", minimum=1)
    cadence_tolerance_hours = _require_int(data, "cadence_tolerance_hours", "collector", minimum=0)
    if min_interval_hours > max_interval_hours:
        raise ValueError("collector.min_interval_hours must not exceed max_interval_hours")
    for label, hours in list(due_hours.items()) + list(area_due_hours.items()):
        if not min_interval_hours <= hours <= max_interval_hours:
            raise ValueError(
                f"collector cadence {label}: {hours}h is outside [{min_interval_hours}, "
                f"{max_interval_hours}]")
    skip_slices = frozenset(
        (str(entry["role"]), str(area)) for entry in data.get("skip_slices", []) for area in entry["areas"])
    return CollectorConfig(
        due_hours=due_hours,
        max_pages_per_slice=int(data.get("max_pages_per_slice", 90)),
        known_pages_to_stop=int(data.get("known_pages_to_stop", 3)),
        window_margin_hours=int(data.get("window_margin_hours", 2)),
        legacy_lock_path=data.get("legacy_lock_path"),
        area_due_hours=area_due_hours,
        area_max_pages=area_max_pages,
        refused_pages_to_defer=refused_pages_to_defer,
        min_interval_hours=min_interval_hours,
        max_interval_hours=max_interval_hours,
        cadence_tolerance_hours=cadence_tolerance_hours,
        skip_slices=skip_slices,
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


def build_slices(tier_configs: dict[int, dict], due_hours: dict[int, int],
                 area_due_hours: dict[str, int] | None = None,
                 skip_slices: frozenset | set | None = None,
                 area_max_pages: dict[str, int] | None = None) -> list[SliceSpec]:
    """One slice per (role, area). `area_due_hours` overrides the tier's cadence for an area;
    `skip_slices` drops (role, area) pairs. Naming an area or pair that no tier config produces
    is an error, so a typo cannot silently leave a slice at full cost."""
    area_due_hours = area_due_hours or {}
    area_max_pages = area_max_pages or {}
    skip_slices = set(skip_slices or ())
    specs: list[SliceSpec] = []
    seen: set[tuple[str, str]] = set()
    for tier in sorted(tier_configs):
        search = tier_configs[tier]["search"]
        for area in _areas(search):
            hours = area_due_hours.get(area, due_hours[tier])
            for role in search["roles"]:
                if (role, area) in seen:
                    continue
                seen.add((role, area))
                if (role, area) in skip_slices:
                    continue
                specs.append(SliceSpec(SOURCE, role, area, tier, hours * 3600))
    known_areas = {area for _role, area in seen}
    unknown = sorted((set(area_due_hours) - known_areas)
                     | (set(area_max_pages) - known_areas)
                     | {f"{role} / {area}" for role, area in skip_slices - seen})
    if unknown:
        raise ValueError(f"collector config names slices no tier config produces: {unknown}")
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
                # A slice that was walking at its old base follows the new one in either direction;
                # one the adaptive cadence had already shortened only ever follows it down.
                cur.execute(
                    "UPDATE slices SET tier = %s, base_interval_seconds = %s, active = TRUE, "
                    "interval_seconds = CASE WHEN interval_seconds = %s THEN %s "
                    "ELSE LEAST(interval_seconds, %s) END WHERE id = %s",
                    (spec.tier, spec.base_interval_seconds, base, spec.base_interval_seconds,
                     spec.base_interval_seconds, sid))
                counts["updated"] += 1
        for key, (sid, _tier, _base, active) in existing.items():
            if key not in wanted and active:
                cur.execute("UPDATE slices SET active = FALSE WHERE id = %s", (sid,))
                counts["deactivated"] += 1
    return counts


def clamp_cadence(conn, source: str, min_seconds: int, max_seconds: int) -> int:
    """Pull every active slice's interval into [min, max] and move its due time in
    with it. The deploy clamp for the adaptive cadence; only rows that change are
    touched, and the caller commits."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE slices SET interval_seconds = LEAST(GREATEST(interval_seconds, %s), %s), "
            "due_at = LEAST(due_at, COALESCE(last_walked_at, due_at) "
            "+ LEAST(GREATEST(interval_seconds, %s), %s) * interval '1 second') "
            "WHERE source = %s AND active AND (interval_seconds < %s OR interval_seconds > %s "
            "OR (due_at > COALESCE(last_walked_at, due_at) "
            "+ LEAST(GREATEST(interval_seconds, %s), %s) * interval '1 second' "
            "AND (stop_reason IS NULL "
            "OR stop_reason <> ALL(ARRAY['rate_limited','error','throttled_empty']))))",
            (min_seconds, max_seconds, min_seconds, max_seconds, source, min_seconds, max_seconds,
             min_seconds, max_seconds))
        return cur.rowcount
