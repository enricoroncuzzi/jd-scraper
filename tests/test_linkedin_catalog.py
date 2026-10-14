from collections import Counter

from src.linkedin.catalog import (
    SliceSpec, build_slices, load_collector_config, load_tier_configs, sync_slices,
)


def _real_specs():
    config = load_collector_config()
    return build_slices(load_tier_configs(), config.due_hours)


def test_the_real_configs_give_132_slices_across_the_four_tiers():
    specs = _real_specs()
    assert Counter(s.tier for s in specs) == {1: 4, 2: 8, 3: 116, 4: 4}
    assert len(specs) == 132 == len({(s.role, s.area) for s in specs})


def test_tier_areas_are_the_ones_the_old_searches_used():
    areas = {tier: {s.area for s in _real_specs() if s.tier == tier} for tier in (1, 2, 3, 4)}
    assert areas[1] == {"Italy"}
    assert areas[2] == {"Switzerland", "San Marino, San Marino"}
    assert areas[4] == {"United Kingdom"}
    assert "Italy" not in areas[3] and len(areas[3]) == 29
    assert {"Germany", "Czechia", "Norway"} <= areas[3]


def test_base_cadence_follows_the_tier():
    cadence = {s.tier: s.base_interval_seconds for s in _real_specs()}
    assert cadence == {1: 4 * 3600, 2: 6 * 3600, 3: 12 * 3600, 4: 12 * 3600}


def test_the_roles_are_the_four_current_searches():
    assert {s.role for s in _real_specs()} == {
        "AI Engineer", "ML Engineer", "Data Scientist", "Software Engineer AI"}


def test_sync_adds_updates_and_deactivates(store_conn):
    first = [SliceSpec("linkedin", "AI Engineer", "Italy", 1, 14400),
             SliceSpec("linkedin", "ML Engineer", "Italy", 1, 14400)]
    assert sync_slices(store_conn, first) == {"added": 2, "updated": 0, "deactivated": 0}
    assert sync_slices(store_conn, first) == {"added": 0, "updated": 0, "deactivated": 0}
    second = [SliceSpec("linkedin", "AI Engineer", "Italy", 1, 7200)]
    assert sync_slices(store_conn, second) == {"added": 0, "updated": 1, "deactivated": 1}
    with store_conn.cursor() as cur:
        cur.execute("SELECT role, base_interval_seconds, interval_seconds, active FROM slices ORDER BY role")
        assert cur.fetchall() == [("AI Engineer", 7200, 7200, True), ("ML Engineer", 14400, 14400, False)]


def test_slices_of_different_sources_are_synced_independently(store_conn):
    sync_slices(store_conn, [SliceSpec("linkedin", "AI Engineer", "Italy", 1, 14400)])
    sync_slices(store_conn, [SliceSpec("freehire", "ai_engineering", "countries=it", 1, 86400)],
                source="freehire")
    assert sync_slices(store_conn, [SliceSpec("linkedin", "AI Engineer", "Italy", 1, 14400)]) == {
        "added": 0, "updated": 0, "deactivated": 0}
    with store_conn.cursor() as cur:
        cur.execute("SELECT source, active FROM slices ORDER BY source")
        assert cur.fetchall() == [("freehire", True), ("linkedin", True)]
