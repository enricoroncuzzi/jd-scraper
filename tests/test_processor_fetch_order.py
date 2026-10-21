from src.processor import cheap, titles


def test_the_title_matcher_lives_in_one_place_and_cheap_re_exports_it():
    assert cheap.title_hits_exclude is titles.title_hits_exclude
    assert titles.title_hits_exclude("Senior AI Engineer (C++)", ("C++", "Java")) == "C++"
    assert titles.title_hits_exclude("JavaScript Engineer", ("Java",)) is None


from dataclasses import replace

import pytest

from src.processor.fetch_order import FetchOrder, FetchOrderClass, TierOrder, title_kind

AI = ("AI", "data scientist")
SENIOR = ("Senior", "Lead")
FOUR = TierOrder(classes=(FetchOrderClass("ai_country", "ai", "country_only"),
                          FetchOrderClass("ai_city", "ai", "other"),
                          FetchOrderClass("other_country", "other", "country_only"),
                          FetchOrderClass("other_city", "other", "other")))
TWO = TierOrder(classes=(FetchOrderClass("ai", "ai"), FetchOrderClass("other", "other")))
ORDER = FetchOrder(enabled=True, quota_one_in=4, backfill_per_tick=500,
                   ai_title_keywords=AI, senior_title_keywords=SENIOR, default=FOUR,
                   by_tier={2: TWO})


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
