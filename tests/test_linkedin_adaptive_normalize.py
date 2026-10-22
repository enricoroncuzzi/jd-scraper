from src.linkedin.adaptive import normalize_state
from src.linkedin.schedule import AdaptivePolicy

POLICY = AdaptivePolicy(enabled=True)


def test_a_corrupt_state_degrades_to_blank():
    for loaded in (None, {}, {"version": 99, "level": "x", "levels": 3},
                   {"version": 1, "level": "x", "levels": 3, "trailing": "nope"}):
        state = normalize_state(loaded, POLICY)
        assert state["version"] == 1 and state["level"] == 1
        assert state["trailing"] == [] and state["brake"] is None


def test_a_partial_state_is_filled_from_the_blank_one():
    state = normalize_state({"version": 1, "active": True, "level": 3,
                             "levels": {"3": {"requests": 5}}}, POLICY)
    assert state["levels"]["3"]["requests"] == 5
    assert state["levels"]["3"]["cooldown_hours"] == 2
    assert state["levels"]["1"]["requests"] == 0


def test_unknown_level_keys_are_dropped():
    state = normalize_state({"version": 1, "levels": {"9": {"requests": 1}}}, POLICY)
    assert "9" not in state["levels"]
