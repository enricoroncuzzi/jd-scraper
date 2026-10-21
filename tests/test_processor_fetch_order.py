from src.processor import cheap, titles


def test_the_title_matcher_lives_in_one_place_and_cheap_re_exports_it():
    assert cheap.title_hits_exclude is titles.title_hits_exclude
    assert titles.title_hits_exclude("Senior AI Engineer (C++)", ("C++", "Java")) == "C++"
    assert titles.title_hits_exclude("JavaScript Engineer", ("Java",)) is None
