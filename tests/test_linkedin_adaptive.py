from datetime import datetime, timedelta, timezone

from src.linkedin.adaptive import (
    MAX_LEVELS, Signals, above_3x_guard, above_3x_status, blank_state, climb_blockers,
    current_level, effective_spacing, hold_level, is_above_speed, level_text,
    normalize_state, parse_iso, paused_day, review, rolling_24h_requests,
    speed_multiple, wilson_upper,
)
from src.linkedin.schedule import AdaptivePolicy

NOW = datetime(2026, 10, 20, 4, 30, tzinfo=timezone.utc)
POLICY = AdaptivePolicy(enabled=True)
HIGH = Signals(demand_waiting=5000, llm_requests_today=100, llm_day=NOW.date(),
               llm_stop_requests=800, refreshed_at=None)


def feed(events, *, start_level=1, policy=POLICY, signals=HIGH, start=None):
    state = start if start is not None else {
        **blank_state(policy), "active": True, "level": start_level}
    for offset, (outcome, block) in enumerate(events):
        at = NOW + timedelta(seconds=75 * offset)
        state = review(state, policy, at, day=at.date(), outcome=outcome,
                       level=current_level(state), block=block, signals=signals)
    return state


def _level(state, level):
    return state["levels"][str(level)]


def test_off_switch_returns_the_base_spacing_and_keeps_the_memory():
    policy = AdaptivePolicy(enabled=False)
    assert effective_spacing(policy, {"active": True, "level": 5}) == 75
    assert effective_spacing(policy, {"active": True, "level": 5}, base=91) == 91
    state = {**blank_state(policy), "active": True, "level": 4}
    state["levels"]["3"]["cooldown_hours"] = 8
    result = review(state, policy, NOW, day=NOW.date(), outcome="clean", level=4)
    assert result["active"] is False and result["level"] == 1
    assert result["trailing"] == [] and result["brake"] is None
    assert result["levels"]["3"]["cooldown_hours"] == 8


def test_the_eight_levels_map_to_their_spacings_and_speeds():
    assert [effective_spacing(POLICY, {"active": True, "level": n}) for n in range(1, 9)] == [
        75, 50, 37.5, 30, 25, 18.75, 15, 10.7]
    assert [round(speed_multiple(POLICY, n), 1) for n in range(1, 9)] == [
        1, 1.5, 2, 2.5, 3, 4, 5, 7]
    assert level_text(POLICY, 6) == "6/8 (18.75s, 4x)"
    assert hold_level(POLICY) == 5
    assert is_above_speed(POLICY, 6, 3.0) and not is_above_speed(POLICY, 5, 3.0)
    assert is_above_speed(POLICY, 7, 4.0) and not is_above_speed(POLICY, 6, 4.0)


def test_a_climb_above_three_x_needs_600_requests_at_the_level():
    state = {**blank_state(POLICY), "active": True, "level": 5}
    state["levels"]["4"] = _stint(successes=100)
    state["levels"]["5"] = _stint(requests=599, successes=599)
    assert "599/600 requests at this level" in above_3x_guard(POLICY, state, NOW, HIGH)
    state["levels"]["5"] = _stint(requests=600, successes=600)
    assert above_3x_guard(POLICY, state, NOW, HIGH) is None
    climbed = review(state, POLICY, NOW, day=NOW.date(), outcome="clean", level=5,
                     signals=HIGH)
    assert current_level(climbed) == 6
    assert climbed["day_above_3x_climbs"] == 1


def test_only_one_climb_above_three_x_per_rome_day():
    state = {**blank_state(POLICY), "active": True, "level": 6,
             "day": NOW.date().isoformat(), "day_above_3x_climbs": 1}
    state["levels"]["5"] = _stint(successes=300)
    state["levels"]["6"] = _stint(requests=600, successes=600)
    assert "climb already used today (1/1)" in above_3x_guard(POLICY, state, NOW, HIGH)
    tomorrow = NOW + timedelta(days=1)
    state["day"] = None
    climbed = review(state, POLICY, tomorrow, day=tomorrow.date(), outcome="clean",
                     level=6, signals=HIGH)
    assert current_level(climbed) == 7 and climbed["day_above_3x_climbs"] == 1


def test_the_rolling_24h_ceiling_holds_the_ladder_at_three_x():
    hour = NOW.replace(minute=0, second=0, microsecond=0)
    state = {**blank_state(POLICY), "active": True, "level": 6,
             "day": NOW.date().isoformat(),
             "window": [[hour.isoformat(), 5000]]}
    state["levels"]["5"] = _stint(successes=300)
    state["levels"]["6"] = _stint(requests=600, successes=600)
    assert above_3x_guard(POLICY, state, NOW, HIGH).startswith("rolling 24h ")
    dropped = review(state, POLICY, NOW + timedelta(seconds=25), day=NOW.date(),
                     outcome="clean", level=6, signals=HIGH)
    assert current_level(dropped) == 5
    assert dropped["last_move"]["kind"] == "ceiling"


def test_the_rolling_window_forgets_buckets_older_than_a_day():
    hour = NOW.replace(minute=0, second=0, microsecond=0)
    state = {**blank_state(POLICY), "window": [
        [(hour - timedelta(hours=25)).isoformat(), 400],
        [(hour - timedelta(hours=23)).isoformat(), 100]]}
    assert rolling_24h_requests(state, NOW) == 100
    state = review({**state, "active": True, "level": 1}, POLICY, NOW, day=NOW.date(),
                   outcome="clean", level=1, signals=HIGH)
    assert rolling_24h_requests(state, NOW) == 101
    assert [key for key, _count in state["window"]] == [
        (hour - timedelta(hours=23)).isoformat(), hour.isoformat()]


def test_the_rolling_window_keeps_a_bucket_whose_hour_is_still_inside_the_day():
    hour = NOW.replace(minute=0, second=0, microsecond=0)
    now = hour + timedelta(minutes=30)
    inside = {**blank_state(POLICY),
              "window": [[(hour - timedelta(hours=24)).isoformat(), 1000]]}
    assert rolling_24h_requests(inside, now) == 1000
    outside = {**blank_state(POLICY),
               "window": [[(hour - timedelta(hours=25)).isoformat(), 1000]]}
    assert rolling_24h_requests(outside, now) == 0


def test_a_level_above_three_x_drops_to_three_x_when_the_queue_empties():
    quiet = Signals(demand_waiting=100, llm_requests_today=100, llm_stop_requests=800)
    state = {**blank_state(POLICY), "active": True, "level": 7}
    dropped = review(state, POLICY, NOW, day=NOW.date(), outcome="neutral", level=7,
                     signals=quiet)
    assert current_level(dropped) == 5
    assert dropped["last_move"]["kind"] == "demand"
    assert dropped["last_move"]["reason"] == "queue empty above 3x"
    held = review(state, POLICY, NOW, day=NOW.date(), outcome="neutral", level=7,
                  signals=HIGH)
    assert current_level(held) == 7


def test_climbing_above_four_x_needs_openrouter_headroom():
    state = {**blank_state(POLICY), "active": True, "level": 6,
             "day": NOW.date().isoformat()}
    state["levels"]["5"] = _stint(successes=300)
    state["levels"]["6"] = _stint(requests=600, successes=600)
    tight = Signals(demand_waiting=5000, llm_requests_today=600, llm_stop_requests=800)
    assert above_3x_guard(POLICY, state, NOW, tight) == "OpenRouter 600/600 above 4x"
    roomy = Signals(demand_waiting=5000, llm_requests_today=599, llm_stop_requests=800)
    assert above_3x_guard(POLICY, state, NOW, roomy) is None
    at_four = {**state, "level": 5, "levels": {
        **state["levels"], "5": _stint(requests=600, successes=600)}}
    assert [b for b in climb_blockers(POLICY, at_four, NOW, tight) if "above 4x" in b] == []


def test_a_protect_drop_from_above_three_x_never_lands_above_three_x():
    hot = ([("clean", None)] * 14 + [("refused", None), ("clean", None)] * 5
           + [("refused", None)])
    assert current_level(feed(hot, start_level=7)) == 5
    assert current_level(feed([("refused", None), ("clean", None)] * 5,
                              start_level=8)) == 5
    assert current_level(feed([("refused", None), ("clean", None)] * 5,
                              start_level=6)) == 4


def test_a_drop_or_brake_from_above_three_x_blocks_above_three_x_for_a_day():
    dropped = feed([("refused", None), ("clean", None)] * 5, start_level=8)
    hold = parse_iso(dropped["above_3x_hold_until"])
    assert hold is not None and hold > NOW
    dropped["levels"]["5"] = _stint(requests=600, successes=600)
    assert above_3x_guard(POLICY, dropped, NOW + timedelta(hours=23), HIGH).startswith(
        "held until ")
    assert above_3x_guard(POLICY, dropped, NOW + timedelta(hours=25), HIGH) is None
    braked = feed([("clean", None), ("neutral", "http_999")], start_level=7)
    assert current_level(braked) == 1
    assert parse_iso(braked["above_3x_hold_until"]) is not None


def test_the_configured_ceiling_caps_the_ladder():
    capped = AdaptivePolicy(enabled=True, max_level=6)
    state = {**blank_state(capped), "active": True, "level": 6}
    state["levels"]["5"] = _stint(successes=300)
    state["levels"]["6"] = _stint(requests=600, successes=600)
    assert climb_blockers(capped, state, NOW, HIGH) == ["top level"]
    assert current_level(review(state, capped, NOW, day=NOW.date(), outcome="clean",
                                level=6, signals=HIGH)) == 6


def test_lowering_the_ceiling_drops_the_stored_level():
    capped = AdaptivePolicy(enabled=True, max_level=6)
    assert normalize_state({**blank_state(POLICY), "level": 8}, capped)["level"] == 6


def test_a_policy_shorter_than_the_stored_level_still_spaces_the_request():
    five = AdaptivePolicy(enabled=True, levels_seconds=(75, 50, 37.5, 30, 25), max_level=5)
    assert effective_spacing(five, {"active": True, "level": 8}) == 25
    assert len(blank_state()["levels"]) == MAX_LEVELS


def test_a_level_above_three_x_keeps_the_brake_and_the_daily_pause():
    braked = feed([("clean", None), ("neutral", "http_999")], start_level=7)
    assert current_level(braked) == 1
    assert braked["brake"]["reason"] == "http_999"
    paused = review(braked, POLICY, NOW + timedelta(minutes=62), day=NOW.date(),
                    outcome="neutral", level=1, block="http_999", signals=HIGH)
    assert paused_day(paused) == NOW.date()


def test_above_three_x_status_names_the_guard_or_says_why_it_does_not_apply():
    signals = {"demand": 5000, "llm": 100, "llm_day": NOW.date().isoformat(),
               "llm_stop": 800, "at": NOW.isoformat()}
    assert above_3x_status(POLICY, {**blank_state(POLICY), "active": True, "level": 4},
                           NOW) == "n/a"
    assert above_3x_status(POLICY, {**blank_state(POLICY), "active": True, "level": 8},
                           NOW) == "at the ceiling"
    used = {**blank_state(POLICY), "active": True, "level": 5, "signals": signals,
            "day": NOW.date().isoformat(), "day_above_3x_climbs": 1}
    used["levels"]["5"] = _stint(requests=600, successes=600)
    assert above_3x_status(POLICY, used, NOW) == "climb already used today (1/1)"
    ready = {**blank_state(POLICY), "active": True, "level": 5, "signals": signals,
             "day": NOW.date().isoformat()}
    ready["levels"]["5"] = _stint(requests=600, successes=600)
    assert above_3x_status(POLICY, ready, NOW) == "ready"


def test_a_refusal_cluster_above_three_x_is_remembered_when_the_queue_empties_mid_cluster():
    # The holds return, so if they ran before the protect drop a queue crossing on the tenth
    # request would land the ladder at 3x with no memory of the refusals. The drop must win.
    quiet = Signals(demand_waiting=100, llm_requests_today=100, llm_day=NOW.date(),
                    llm_stop_requests=800, refreshed_at=None)
    state = feed([("refused", None), ("clean", None)] * 4 + [("refused", None)],
                 start_level=7)
    assert current_level(state) == 7          # nine requests: below the protect sample
    dropped = feed([("clean", None)], start=state, signals=quiet)
    assert dropped["last_move"]["kind"] == "drop"
    assert parse_iso(dropped["above_3x_hold_until"]) is not None
    assert parse_iso(dropped["levels"]["7"]["blocked_until"]) is not None


def test_thirty_percent_trailing_refusals_drop_one_level():
    events = ([("clean", None)] * 14
              + [("refused", None), ("clean", None)] * 5
              + [("refused", None)])
    state = feed(events, start_level=3)
    assert current_level(state) == 2
    assert state["last_move"]["kind"] == "drop"
    assert state["last_move"]["reason"] == "protect: trailing 20 refused 30%"
    assert state["trailing"] == []


def test_fifty_percent_trailing_refusals_drop_two_levels():
    state = feed([("refused", None), ("clean", None)] * 5, start_level=4)
    assert current_level(state) == 2
    assert state["last_move"]["reason"] == "protect: trailing 20 refused 50%"


def test_a_share_needs_the_minimum_sample():
    state = feed([("refused", None), ("clean", None)] * 4 + [("refused", None)],
                 start_level=4)
    assert current_level(state) == 4 and state["last_move"] is None


def test_four_in_a_row_drops_two_levels():
    state = feed([("clean", None)] * 16 + [("refused", None)] * 4, start_level=4)
    assert current_level(state) == 2
    assert state["last_move"]["reason"] == "protect: 4 refusals in a row"


def test_a_404_neutral_never_counts_as_a_refusal():
    state = feed([("neutral", None)] * 10 + [("clean", None)] * 10, start_level=3)
    assert current_level(state) == 3
    assert state["trailing"].count("refused") == 0
    state = feed([("neutral", None)] * 4, start=state)
    assert current_level(state) == 3


def test_the_protect_window_is_the_config_value():
    policy = AdaptivePolicy(enabled=True, protect_window_requests=10, protect_min_requests=5)
    state = feed([("clean", None)] * 7 + [("refused", None)] * 3,
                 start_level=3, policy=policy)
    assert current_level(state) == 2
    assert state["last_move"]["reason"] == "protect: trailing 10 refused 30%"


def test_a_drop_remembers_the_level_for_two_hours_then_four():
    state = feed([("refused", None)] * 4, start_level=3)
    drop_at = NOW + timedelta(seconds=75 * 3)
    assert current_level(state) == 1
    assert parse_iso(_level(state, 3)["blocked_until"]) == drop_at + timedelta(hours=2)
    assert _level(state, 3)["cooldown_hours"] == 4
    at_level_2 = {**state, "level": 2}
    assert any("level 3 blocked until" in blocker
               for blocker in climb_blockers(POLICY, at_level_2, NOW, HIGH))
    second = {**blank_state(POLICY), "active": True, "level": 3}
    second["levels"]["3"]["cooldown_hours"] = 4
    second = feed([("refused", None)] * 4, start=second)
    assert parse_iso(_level(second, 3)["blocked_until"]) == drop_at + timedelta(hours=4)
    assert _level(second, 3)["cooldown_hours"] == 8


def test_a_clean_day_halves_the_memory():
    state = {**blank_state(POLICY), "active": True, "level": 1, "day": "2026-10-19",
             "day_requests": 100, "day_drops": 0, "day_brakes": 0}
    state["levels"]["3"]["cooldown_hours"] = 8
    result = review(state, POLICY, NOW, day=NOW.date(), outcome="clean", level=1,
                    signals=HIGH)
    assert result["levels"]["3"]["cooldown_hours"] == 4
    state["day_drops"] = 1
    result = review(state, POLICY, NOW, day=NOW.date(), outcome="clean", level=1,
                    signals=HIGH)
    assert result["levels"]["3"]["cooldown_hours"] == 8


def test_a_level_re_climbed_after_a_drop_still_halves_its_wait():
    dropped = feed([("refused", None)] * 4, start_level=3)
    assert current_level(dropped) == 1
    assert _level(dropped, 3)["cooldown_hours"] == 4
    dropped["level"] = 2
    dropped["levels"]["2"] = _stint(requests=200)
    later = NOW + timedelta(hours=3)
    reclimbed = review(dropped, POLICY, later, day=later.date(), outcome="clean", level=2,
                       signals=HIGH, throughput_check=False)
    assert current_level(reclimbed) == 3
    assert _level(reclimbed, 3)["cooldown_hours"] == 4
    state = {**reclimbed, "day": "2026-10-19", "day_requests": 200,
             "day_drops": 0, "day_brakes": 0}
    result = review(state, POLICY, later, day=later.date(), outcome="clean", level=3,
                    signals=HIGH)
    assert result["levels"]["3"]["cooldown_hours"] == 2


def test_the_memory_floor_is_the_first_wait():
    state = {**blank_state(POLICY), "active": True, "level": 1, "day": "2026-10-19",
             "day_requests": 100, "day_drops": 0, "day_brakes": 0}
    state["levels"]["3"]["cooldown_hours"] = 2
    result = review(state, POLICY, NOW, day=NOW.date(), outcome="clean", level=1,
                    signals=HIGH)
    assert result["levels"]["3"]["cooldown_hours"] == 2


def test_climb_needs_200_requests_and_a_low_cautious_upper_bound():
    state = feed([("clean", None)] * 199, start_level=1)
    assert any("requests at this level" in blocker
               for blocker in climb_blockers(POLICY, state, NOW, HIGH))
    state = feed([("clean", None)] * 200, start_level=1)
    assert current_level(state) == 2
    refusals = set(range(0, 198, 11))
    state = feed([("refused", None) if i in refusals else ("clean", None)
                  for i in range(200)], start_level=1)
    assert current_level(state) == 1
    refusals = set(range(0, 200, 20))
    state = feed([("refused", None) if i in refusals else ("clean", None)
                  for i in range(200)], start_level=1)
    assert current_level(state) == 2


def _stint(requests=200, successes=200, refusals=0, seconds=15000.0):
    return {"requests": requests, "refusals": refusals, "successes": successes,
            "active_seconds": seconds, "first_at": None, "last_at": None,
            "cooldown_hours": 2.0, "blocked_until": None}


def test_the_climb_never_fires_on_a_refused_request_or_inside_a_streak():
    state = feed([("clean", None)] * 195 + [("refused", None)] * 5, start_level=1)
    assert _level(state, 1)["requests"] == 200
    assert current_level(state) == 1
    state = feed([("clean", None)], start=state)
    assert current_level(state) == 1
    state = feed([("clean", None)] * 2, start=state)
    assert current_level(state) == 1
    state = feed([("clean", None)], start=state)
    assert current_level(state) == 2
    assert state["last_move"]["kind"] == "climb"


def test_climb_requires_more_successes_per_hour_than_the_level_below():
    state = {**blank_state(POLICY), "active": True, "level": 2}
    state["levels"]["1"] = _stint(successes=100)
    state["levels"]["2"] = _stint(successes=200)
    assert not [b for b in climb_blockers(POLICY, state, NOW, HIGH)
                if "successes/hour" in b]
    state["levels"]["1"] = _stint(successes=300)
    assert any("successes/hour" in blocker
               for blocker in climb_blockers(POLICY, state, NOW, HIGH))
    state["levels"]["2"] = _stint(successes=0, seconds=0.0)
    assert any("successes/hour" in blocker
               for blocker in climb_blockers(POLICY, state, NOW, HIGH))


def test_climb_is_blocked_by_small_demand_and_openrouter_headroom():
    state = {**blank_state(POLICY), "active": True, "level": 2}
    state["levels"]["1"] = _stint(successes=100)
    state["levels"]["2"] = _stint(successes=200)
    assert not climb_blockers(POLICY, state, NOW, HIGH)
    low = Signals(demand_waiting=999, llm_requests_today=100, llm_stop_requests=800,
                  refreshed_at=None)
    assert any("backlog" in blocker for blocker in climb_blockers(POLICY, state, NOW, low))
    no_room = Signals(demand_waiting=5000, llm_requests_today=800, llm_stop_requests=800,
                      refreshed_at=None)
    assert any("OpenRouter" in blocker
               for blocker in climb_blockers(POLICY, state, NOW, no_room))


def test_missing_or_stale_signals_block_the_climb_and_easing():
    state = {**blank_state(POLICY), "active": True, "level": 4}
    state["levels"]["3"] = _stint(successes=100)
    state["levels"]["4"] = _stint(successes=200)
    assert any("signals unavailable" in blocker
               for blocker in climb_blockers(POLICY, state, NOW, None))
    result = review(state, POLICY, NOW, day=NOW.date(), outcome="clean", level=4,
                    signals=None)
    assert current_level(result) == 4 and result["last_ease_at"] is None
    stale = Signals(demand_waiting=5000, llm_requests_today=100, llm_stop_requests=800,
                    refreshed_at=NOW - timedelta(minutes=11))
    assert any("signals unavailable" in blocker
               for blocker in climb_blockers(POLICY, state, NOW, stale))
    fresh_edge = Signals(demand_waiting=5000, llm_requests_today=100,
                         llm_stop_requests=800,
                         refreshed_at=NOW - timedelta(minutes=10))
    assert not [b for b in climb_blockers(POLICY, state, NOW, fresh_edge)
                if "signals unavailable" in b]


def test_a_block_signal_brakes_to_150s_and_freezes_climbing():
    state = {**blank_state(POLICY), "active": True, "level": 3}
    state = review(state, POLICY, NOW, day=NOW.date(), outcome="neutral", level=3,
                   block="http_999", signals=HIGH)
    assert state["brake"] is not None and current_level(state) == 1
    assert effective_spacing(POLICY, state) == 150
    assert climb_blockers(POLICY, state, NOW, HIGH) == ["climbing frozen"]


def test_a_single_connection_error_does_not_brake_but_three_do():
    state = feed([("neutral", "connection")] * 2, start_level=3)
    assert state["brake"] is None and current_level(state) == 3
    state = feed([("neutral", "connection")] * 3, start_level=3)
    assert state["brake"] is not None and current_level(state) == 1


def test_a_connection_error_older_than_the_window_does_not_count():
    state = {**blank_state(POLICY), "active": True, "level": 3}
    for minutes in (0, 20, 40):
        state = review(state, POLICY, NOW + timedelta(minutes=minutes), day=NOW.date(),
                       outcome="neutral", level=3, block="connection", signals=HIGH)
    assert state["brake"] is None
    for minutes in (41, 42):
        state = review(state, POLICY, NOW + timedelta(minutes=minutes), day=NOW.date(),
                       outcome="neutral", level=3, block="connection", signals=HIGH)
    assert state["brake"] is not None


def test_a_brake_clear_needs_20_clean_and_neutrals_neither_count_nor_reset():
    state = {**blank_state(POLICY), "active": True, "level": 3}
    state = review(state, POLICY, NOW, day=NOW.date(), outcome="neutral", level=3,
                   block="http_999", signals=HIGH)
    for offset in range(10):
        state = review(state, POLICY, NOW + timedelta(seconds=75 * (offset + 1)),
                       day=NOW.date(), outcome="clean", level=1, signals=HIGH)
    state = review(state, POLICY, NOW + timedelta(seconds=900), day=NOW.date(),
                   outcome="neutral", level=1, signals=HIGH)
    for offset in range(9):
        state = review(state, POLICY, NOW + timedelta(seconds=75 * (offset + 11)),
                       day=NOW.date(), outcome="clean", level=1, signals=HIGH)
    assert state["brake"] is not None
    state = review(state, POLICY, NOW + timedelta(seconds=2000), day=NOW.date(),
                   outcome="clean", level=1, signals=HIGH)
    assert state["brake"] is None and current_level(state) == 1
    state = {**blank_state(POLICY), "active": True, "level": 3}
    state = review(state, POLICY, NOW, day=NOW.date(), outcome="neutral", level=3,
                   block="http_999", signals=HIGH)
    for offset in range(19):
        state = review(state, POLICY, NOW + timedelta(seconds=75 * (offset + 1)),
                       day=NOW.date(), outcome="clean", level=1, signals=HIGH)
    state = review(state, POLICY, NOW + timedelta(seconds=1500), day=NOW.date(),
                   outcome="refused", level=1, signals=HIGH)
    for offset in range(19):
        state = review(state, POLICY, NOW + timedelta(seconds=75 * (offset + 20)),
                       day=NOW.date(), outcome="clean", level=1, signals=HIGH)
    assert state["brake"] is not None
    state = review(state, POLICY, NOW + timedelta(seconds=3000), day=NOW.date(),
                   outcome="clean", level=1, signals=HIGH)
    assert state["brake"] is None


def test_a_block_persisting_for_an_hour_pauses_the_day():
    state = {**blank_state(POLICY), "active": True, "level": 3}
    state = review(state, POLICY, NOW, day=NOW.date(), outcome="neutral", level=3,
                   block="http_999", signals=HIGH)
    state = review(state, POLICY, NOW + timedelta(minutes=61), day=NOW.date(),
                   outcome="neutral", level=1, block="http_999", signals=HIGH)
    assert paused_day(state) == NOW.date()
    again = review(state, POLICY, NOW + timedelta(minutes=62), day=NOW.date(),
                   outcome="neutral", level=1, block="http_999", signals=HIGH)
    assert paused_day(again) == NOW.date()


def test_a_block_after_a_clean_stretch_does_not_pause_the_day():
    state = {**blank_state(POLICY), "active": True, "level": 3}
    state = review(state, POLICY, NOW, day=NOW.date(), outcome="neutral", level=3,
                   block="http_403", signals=HIGH)
    assert state["brake"] is not None
    for offset in range(19):
        state = review(state, POLICY, NOW + timedelta(seconds=150 * (offset + 1)),
                       day=NOW.date(), outcome="clean", level=1, signals=HIGH)
    state = review(state, POLICY, NOW + timedelta(minutes=61), day=NOW.date(),
                   outcome="neutral", level=1, block="http_403", signals=HIGH)
    assert paused_day(state) is None
    assert state["brake"] is not None


def test_a_block_retried_after_a_clean_stretch_does_not_pause_the_day():
    state = {**blank_state(POLICY), "active": True, "level": 3}
    state = review(state, POLICY, NOW, day=NOW.date(), outcome="neutral", level=3,
                   block="http_403", signals=HIGH)
    for offset in range(19):
        state = review(state, POLICY, NOW + timedelta(seconds=150 * (offset + 1)),
                       day=NOW.date(), outcome="clean", level=1, signals=HIGH)
    later = NOW + timedelta(minutes=61)
    state = review(state, POLICY, later, day=NOW.date(), outcome="neutral", level=1,
                   block="http_403", signals=HIGH)
    assert parse_iso(state["brake"]["since"]) == later
    for seconds in (5, 20, 65):
        state = review(state, POLICY, later + timedelta(seconds=seconds),
                       day=NOW.date(), outcome="neutral", level=1,
                       block="http_403", signals=HIGH)
        assert paused_day(state) is None
    assert parse_iso(state["brake"]["since"]) == later


def test_a_retried_block_extends_the_brake_without_a_second_move():
    state = {**blank_state(POLICY), "active": True, "level": 3,
             "day": NOW.date().isoformat()}
    state = review(state, POLICY, NOW, day=NOW.date(), outcome="neutral", level=3,
                   block="http_403", signals=HIGH)
    assert state["day_brakes"] == 1 and state["last_move"]["kind"] == "brake"
    state = review(state, POLICY, NOW + timedelta(seconds=5), day=NOW.date(),
                   outcome="neutral", level=1, block="http_403", signals=HIGH)
    assert state["day_brakes"] == 1
    assert parse_iso(state["last_move"]["at"]) == NOW
    assert state["brake"]["reason"] == "http_403"


def test_the_next_rome_day_clears_the_pause_and_restarts_at_level_1():
    state = {**blank_state(POLICY), "active": True, "level": 3,
             "paused": {"day": "2026-10-20", "reason": "http_999"}}
    later = NOW + timedelta(days=1)
    state = review(state, POLICY, later, day=later.date(), outcome="clean", level=3,
                   signals=HIGH)
    assert paused_day(state) is None
    assert state["brake"] is None and current_level(state) == 1


def test_demand_easing_backs_off_one_level_per_hour():
    state = {**blank_state(POLICY), "active": True, "level": 4}
    state["levels"]["3"] = _stint(requests=5, successes=5)
    low = Signals(demand_waiting=200, llm_requests_today=100, llm_stop_requests=800,
                  refreshed_at=None)
    state = review(state, POLICY, NOW, day=NOW.date(), outcome="clean", level=4,
                   signals=low)
    assert current_level(state) == 3
    assert _level(state, 3)["requests"] == 0 and _level(state, 3)["successes"] == 0
    state = review(state, POLICY, NOW + timedelta(minutes=30), day=NOW.date(),
                   outcome="clean", level=3, signals=low)
    assert current_level(state) == 3
    state = review(state, POLICY, NOW + timedelta(minutes=61), day=NOW.date(),
                   outcome="clean", level=3, signals=low)
    assert current_level(state) == 2


def test_a_move_returns_without_a_second_decision():
    state = feed([("refused", None)] * 4, start_level=3)
    assert state["last_move"]["kind"] == "drop" and current_level(state) == 1


def test_a_hot_window_at_level_1_leaves_the_last_move_and_trailing_alone():
    state = {**blank_state(POLICY), "active": True, "level": 1,
             "trailing": ["refused"] * 10,
             "last_move": {"at": NOW.isoformat(), "kind": "drop", "from": 3, "to": 1,
                            "reason": "protect: trailing 20 refused 30%"}}
    result = review(state, POLICY, NOW, day=NOW.date(), outcome="neutral", level=1,
                    signals=HIGH)
    assert result["last_move"]["kind"] == "drop"
    assert len(result["trailing"]) == 11


def test_a_packaging_neutral_is_not_a_refusal_or_a_connection():
    state = feed([("neutral", None)] * 4, start_level=3)
    assert current_level(state) == 3
    assert state["brake"] is None
    assert state["trailing"] == ["neutral"] * 4


def test_wilson_upper_is_the_95_percent_bound():
    assert wilson_upper(0, 200) < 0.02
    assert wilson_upper(18, 200) > 0.12
    assert wilson_upper(10, 200) < 0.12
