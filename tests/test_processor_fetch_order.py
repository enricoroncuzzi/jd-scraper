from src.processor import cheap, titles


def test_the_title_matcher_lives_in_one_place_and_cheap_re_exports_it():
    assert cheap.title_hits_exclude is titles.title_hits_exclude
    assert titles.title_hits_exclude("Senior AI Engineer (C++)", ("C++", "Java")) == "C++"
    assert titles.title_hits_exclude("JavaScript Engineer", ("Java",)) is None


from dataclasses import replace
from pathlib import Path

import pytest

from src.processor.fetch_order import (DEFAULT_VALUE_RANK, UNRANKED, FetchOrder,
                                       FetchOrderClass, TierOrder, load_fetch_order,
                                       title_kind)

AI = ("AI", "data scientist")
SENIOR = ("Senior", "Lead")
FOUR = TierOrder(classes=(FetchOrderClass("ai_country", "ai", "country_only"),
                          FetchOrderClass("ai_city", "ai", "other"),
                          FetchOrderClass("other_country", "other", "country_only"),
                          FetchOrderClass("other_city", "other", "other")))
TWO = TierOrder(classes=(FetchOrderClass("ai", "ai"), FetchOrderClass("other", "other")))
ORDER = FetchOrder(enabled=True, quota_one_in=4, backfill_per_tick=500,
                   ai_title_keywords=AI, senior_title_keywords=SENIOR, default=FOUR,
                   by_tier={2: TWO}, value_rank=dict(DEFAULT_VALUE_RANK))


def test_the_value_rank_orders_the_classes_by_measured_yield():
    assert [ORDER.rank(1, stored) for stored in range(4)] == [0, 2, 3, 5]
    assert [ORDER.rank(2, stored) for stored in range(2)] == [1, 4]
    assert ORDER.rank(1, None) == UNRANKED          # unclassified sorts last
    assert ORDER.rank(1, 7) == UNRANKED             # a rank no class covers


def test_the_important_classes_are_the_ai_title_ones():
    assert ORDER.important_ranks(1) == (0, 1)
    assert ORDER.important_ranks(2) == (0,)


def test_the_shipped_config_ranks_the_classes_by_measured_yield():
    order = load_fetch_order(json.loads(Path("config/processor.json").read_text())
                             ["description_order"])
    assert order.quota_one_in == 4          # the floor share moves to 8 with the order change
    assert [order.rank(1, stored) for stored in range(4)] == [0, 2, 3, 5]
    assert [order.rank(2, stored) for stored in range(2)] == [1, 4]
    assert order.important_ranks(1) == (0, 1)
    assert order.important_ranks(2) == (0,)


def test_the_title_signal_is_a_non_senior_ai_ml_ds_title():
    assert title_kind("AI Engineer", AI, SENIOR) == "ai"
    assert title_kind("Data Scientist", AI, SENIOR) == "ai"
    assert title_kind("Senior AI Engineer", AI, SENIOR) == "other"     # seniority demotes
    assert title_kind("Training Coordinator", AI, SENIOR) == "other"   # "AI" inside a word
    assert title_kind("Python Developer", AI, SENIOR) == "other"


def test_the_location_signal_is_country_only_or_other():
    assert ORDER.classify(1, "AI Engineer", "Italy") == 0
    assert ORDER.classify(1, "AI Engineer", "Milan, Italy") == 1
    assert ORDER.classify(1, "Python Developer", "Italy") == 2
    assert ORDER.classify(1, "Python Developer", "Milan, Italy") == 3
    assert ORDER.classify(1, "AI Engineer", "Europe") == 1          # not country-only
    assert ORDER.classify(1, "AI Engineer", "N/A") == 1


def test_tier_two_ignores_the_location_signal():
    assert ORDER.classify(2, "AI Engineer", "Zurich, Switzerland") == 0
    assert ORDER.classify(2, "AI Engineer", "Switzerland") == 0
    assert ORDER.classify(2, "Python Developer", "Switzerland") == 1
    assert [c.name for c in ORDER.order_for(2).classes] == ["ai", "other"]
    assert ORDER.order_for(3) is FOUR       # the default covers tiers 1, 3, 4


def test_the_quota_turn_is_one_pick_in_four_per_tier_and_off_means_always():
    assert [ORDER.quota_turn(n) for n in range(6)] == [True, False, False, False, True, False]
    assert all(replace(ORDER, enabled=False).quota_turn(n) for n in range(5))
    assert all(replace(ORDER, quota_one_in=1).quota_turn(n) for n in range(5))
    assert replace(ORDER, quota_one_in=4).quota_turn(0)
    assert not replace(ORDER, quota_one_in=4).quota_turn(1)


def test_an_unknown_rank_prints_as_its_number_and_none_as_unclassified():
    assert ORDER.name(1, None) == "unclassified"
    assert ORDER.name(1, 1) == "ai_city"
    assert ORDER.name(1, 9) == "class9"


import json
import re

from src.processor.config import load_processor_config


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d["description_order"].update(enabled="yes"), "description_order.enabled"),
    (lambda d: d["description_order"].update(quota_one_in=0), "description_order.quota_one_in"),
    (lambda d: d["description_order"].update(quota_one_in=True), "description_order.quota_one_in"),
    (lambda d: d["description_order"].update(backfill_per_tick=0), "description_order.backfill_per_tick"),
    (lambda d: d["description_order"].update(ai_title_keywords=[]), "description_order.ai_title_keywords"),
    (lambda d: d["description_order"]["senior_title_keywords"].append("ai"), "description_order keywords"),
    (lambda d: d["description_order"]["classes"].pop("default"), 'description_order.classes must define'),
    (lambda d: d["description_order"]["classes"].update({"7": []}), "description_order.classes key '7'"),
    (lambda d: d["description_order"]["classes"]["default"].pop(), 'description_order.classes["default"] must cover'),
    (lambda d: d["description_order"]["classes"]["default"][0].update(title="tech"), 'must be "ai" or "other"'),
    (lambda d: d["description_order"]["classes"]["default"][0].update(location="city"), 'must be "country_only" or "other"'),
    (lambda d: d["description_order"]["classes"]["default"][2].update(title="ai"), "must cover every combination"),
    (lambda d: d["description_order"]["classes"]["2"][1].update(location="other"), "must use one key set"),
    (lambda d: d["description_order"]["classes"]["default"][1].update(name="ai_country"), "duplicate class name"),
    (lambda d: d["description_order"].update(value_rank="no"), "description_order.value_rank"),
    (lambda d: d["description_order"]["value_rank"].pop("other_city"),
     "description_order.value_rank must rank class 'other_city'"),
    (lambda d: d["description_order"]["value_rank"].update(other_city=3),
     "description_order.value_rank has a duplicate rank"),
    (lambda d: d["description_order"]["value_rank"].update(other_town=6),
     "description_order.value_rank rates unknown class 'other_town'"),
    (lambda d: d["description_order"]["value_rank"].update(other_city=-1),
     "description_order.value_rank has a non-integer rank"),
])
def test_a_broken_description_order_block_fails_loudly(tmp_path, mutate, message):
    data = json.load(open("config/processor.json"))
    mutate(data)
    path = tmp_path / "processor.json"
    path.write_text(json.dumps(data))
    # re.escape: the messages carry "[" and ".", which `match` reads as a regex
    with pytest.raises(ValueError, match=re.escape(message)):
        load_processor_config(str(path))


def test_a_config_without_the_description_order_key_still_loads(tmp_path):
    data = json.load(open("config/processor.json"))
    del data["description_order"]
    path = tmp_path / "processor.json"
    path.write_text(json.dumps(data))
    cfg = load_processor_config(str(path))
    assert cfg.fetch_order.enabled is False and cfg.fetch_order.quota_one_in == 4
    assert cfg.fetch_order.value_rank == DEFAULT_VALUE_RANK


def test_a_custom_class_list_without_a_rank_fails_loudly(tmp_path):
    # A class name the default table does not rank must be ranked explicitly, or the
    # pick would send the whole class to the back of the queue silently.
    data = json.load(open("config/processor.json"))
    data["description_order"]["classes"]["default"][3]["name"] = "other_town"
    del data["description_order"]["value_rank"]
    path = tmp_path / "processor.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="must rank class 'other_town'"):
        load_processor_config(str(path))
