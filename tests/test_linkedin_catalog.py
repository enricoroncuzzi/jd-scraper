import json
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

from src.linkedin.catalog import (
    SliceSpec, build_slices, clamp_cadence, load_collector_config, load_tier_configs, sync_slices,
)

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
H = 3600


def _real_specs():
    config = load_collector_config()
    return build_slices(load_tier_configs(), config.due_hours, config.area_due_hours, config.skip_slices)


def test_the_real_configs_give_127_slices_across_the_four_tiers():
    specs = _real_specs()
    assert Counter(s.tier for s in specs) == {1: 3, 2: 4, 3: 116, 4: 4}
    assert len(specs) == 127 == len({(s.role, s.area) for s in specs})


def test_the_catalog_before_any_reduction_is_132_slices():
    config = load_collector_config()
    assert len(build_slices(load_tier_configs(), config.due_hours)) == 132


def test_the_reduction_drops_the_redundant_roles_and_keeps_only_ai_engineer_in_san_marino():
    pairs = {(s.role, s.area) for s in _real_specs()}
    assert ("ML Engineer", "Italy") not in pairs and ("ML Engineer", "Switzerland") not in pairs
    assert {role for role, area in pairs if area == "San Marino, San Marino"} == {"AI Engineer"}
    assert {("AI Engineer", "Italy"), ("Data Scientist", "Italy"), ("Software Engineer AI", "Italy"),
            ("ML Engineer", "Germany"), ("ML Engineer", "United Kingdom")} <= pairs


def test_the_shipped_cadences_keep_every_area_within_48_hours():
    cadence = {(s.role, s.area): s.base_interval_seconds // H for s in _real_specs()}
    by_area = {area: hours for (_role, area), hours in cadence.items()}
    assert by_area["Italy"] == 12 and by_area["Switzerland"] == 24 and by_area["United Kingdom"] == 24
    assert by_area["San Marino, San Marino"] == 48 and by_area["Germany"] == 48
    assert by_area["Slovakia"] == 48 and by_area["Greece"] == 24
    assert all(12 <= hours <= 48 for hours in cadence.values())


def test_the_ten_large_areas_are_capped_at_20_pages_and_the_rest_at_90():
    cfg = load_collector_config()
    assert set(cfg.area_max_pages) == {"Belgium", "France", "Germany", "Ireland", "Netherlands",
                                      "Poland", "Portugal", "Romania", "Spain", "Sweden"}
    assert cfg.max_pages_for("Germany") == 20 and cfg.max_pages_for("Italy") == 90


@pytest.mark.parametrize("change", [{"due_hours": {"1": 12, "2": 24, "3": 72, "4": 24}},
                                    {"area_due_hours": {"Malta": 6}},
                                    {"min_interval_hours": 0}])
def test_a_cadence_outside_the_bounds_is_rejected(tmp_path, change):
    data = json.load(open("config/collector.json"))
    data.update(change)
    path = tmp_path / "collector.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_collector_config(str(path))


def test_an_unknown_area_in_the_page_caps_is_an_error():
    with pytest.raises(ValueError, match="Atlantis"):
        build_slices(load_tier_configs(), {1: 12, 2: 24, 3: 48, 4: 24}, area_max_pages={"Atlantis": 20})


def test_the_clamp_pulls_shortened_and_overlong_slices_into_range(store_conn):
    sync_slices(store_conn, [
        SliceSpec("linkedin", "AI Engineer", "Italy", 1, 12 * H),
        SliceSpec("linkedin", "AI Engineer", "Germany", 3, 48 * H),
        SliceSpec("linkedin", "AI Engineer", "France", 3, 24 * H),
    ], NOW)
    with store_conn.cursor() as cur:
        cur.execute("UPDATE slices SET interval_seconds = 3600 WHERE area = 'Italy'")
        cur.execute("UPDATE slices SET last_walked_at = %s, interval_seconds = 72 * 3600, "
                    "due_at = %s + interval '72 hours' WHERE area = 'Germany'", (NOW, NOW))
        cur.execute("UPDATE slices SET interval_seconds = 24 * 3600 WHERE area = 'France'")
    store_conn.commit()
    assert clamp_cadence(store_conn, "linkedin", 12 * H, 48 * H) == 2
    with store_conn.cursor() as cur:
        cur.execute("SELECT area, interval_seconds, due_at FROM slices ORDER BY area")
        rows = {area: (interval, due) for area, interval, due in cur.fetchall()}
    assert rows["Italy"][0] == 12 * H
    assert rows["Germany"][0] == 48 * H and rows["Germany"][1] == NOW + timedelta(hours=48)
    assert rows["France"][0] == 24 * H


def test_a_typo_in_the_overrides_or_skips_is_an_error_not_a_silent_full_cost_slice():
    tiers = load_tier_configs()
    with pytest.raises(ValueError, match="Slovakai"):
        build_slices(tiers, {1: 4, 2: 6, 3: 12, 4: 12}, {"Slovakai": 72})
    with pytest.raises(ValueError, match="ML Enginer / Italy"):
        build_slices(tiers, {1: 4, 2: 6, 3: 12, 4: 12}, None, {("ML Enginer", "Italy")})


def test_tier_areas_are_the_ones_the_old_searches_used():
    areas = {tier: {s.area for s in _real_specs() if s.tier == tier} for tier in (1, 2, 3, 4)}
    assert areas[1] == {"Italy"}
    assert areas[2] == {"Switzerland", "San Marino, San Marino"}
    assert areas[4] == {"United Kingdom"}
    assert "Italy" not in areas[3] and len(areas[3]) == 29
    assert {"Germany", "Czechia", "Norway"} <= areas[3]


def test_base_cadence_follows_the_tier_without_overrides():
    config = load_collector_config()
    cadence = {s.tier: s.base_interval_seconds for s in build_slices(load_tier_configs(), config.due_hours)}
    assert cadence == {1: 12 * 3600, 2: 24 * 3600, 3: 48 * 3600, 4: 24 * 3600}


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


def test_a_dropped_slice_is_retired_but_its_jobs_and_sightings_stay(store_conn):
    from src.jobstore.sightings import CardInput, record_sighting
    from src.linkedin.walk import claim_due_slice

    now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    keep = SliceSpec("linkedin", "AI Engineer", "Italy", 1, 14400)
    drop = SliceSpec("linkedin", "ML Engineer", "Italy", 1, 14400)
    sync_slices(store_conn, [keep, drop], now)
    with store_conn.cursor() as cur:
        cur.execute("SELECT id FROM slices WHERE role = 'ML Engineer'")
        drop_id = cur.fetchone()[0]
    result = record_sighting(store_conn, CardInput(source="linkedin", source_job_id="1", source_url="https://l/1",
                                                   title="t", company="c", slice_id=drop_id), now)
    store_conn.commit()
    assert sync_slices(store_conn, [keep], now) == {"added": 0, "updated": 0, "deactivated": 1}
    store_conn.commit()
    with store_conn.cursor() as cur:
        cur.execute("SELECT active FROM slices WHERE id = %s", (drop_id,))
        assert cur.fetchone() == (False,)
        cur.execute("SELECT slice_id FROM sightings WHERE job_id = %s", (result.job_id,))
        assert cur.fetchone() == (drop_id,)
        cur.execute("SELECT count(*) FROM jobs WHERE id = %s", (result.job_id,))
        assert cur.fetchone() == (1,)
    assert claim_due_slice(store_conn, now).role == "AI Engineer"
    with store_conn.cursor() as cur:
        cur.execute("UPDATE slices SET due_at = %s WHERE role = 'AI Engineer'", (now.replace(year=2030),))
    assert claim_due_slice(store_conn, now) is None  # the retired slice is never due again


def test_a_slice_still_at_its_old_cadence_follows_a_longer_one_at_once(store_conn):
    sync_slices(store_conn, [SliceSpec("linkedin", "AI Engineer", "Malta", 3, 12 * 3600)])
    assert sync_slices(store_conn, [SliceSpec("linkedin", "AI Engineer", "Malta", 3, 72 * 3600)]) == {
        "added": 0, "updated": 1, "deactivated": 0}
    with store_conn.cursor() as cur:
        cur.execute("SELECT base_interval_seconds, interval_seconds FROM slices")
        assert cur.fetchone() == (72 * 3600, 72 * 3600)


def test_a_slice_the_adaptive_cadence_shortened_keeps_its_shorter_interval(store_conn):
    sync_slices(store_conn, [SliceSpec("linkedin", "AI Engineer", "Germany", 3, 12 * 3600)])
    with store_conn.cursor() as cur:
        cur.execute("UPDATE slices SET interval_seconds = %s", (3 * 3600,))  # halved by cap hits
    sync_slices(store_conn, [SliceSpec("linkedin", "AI Engineer", "Germany", 3, 24 * 3600)])
    with store_conn.cursor() as cur:
        cur.execute("SELECT base_interval_seconds, interval_seconds FROM slices")
        assert cur.fetchone() == (24 * 3600, 3 * 3600)


def test_the_shipped_collector_config_defers_after_three_refusals():
    assert load_collector_config().refused_pages_to_defer == 3