from src.tier_scope import TIER3_ALLOWED_COUNTRIES, is_in_scope, resolve_country


def test_resolves_country_from_city_region_country():
    assert resolve_country("Berlin, Berlin, Germany") == "germany"


def test_resolves_country_from_city_country():
    assert resolve_country("Madrid, Spain") == "spain"


def test_resolves_bare_country():
    assert resolve_country("Netherlands") == "netherlands"


def test_resolves_country_aliases():
    assert resolve_country("Prague, Czech Republic") == "czechia"
    assert resolve_country("London, England, United Kingdom") == "united kingdom"
    assert resolve_country("Edinburgh, Scotland") == "united kingdom"


def test_resolution_is_case_and_space_insensitive():
    assert resolve_country("  MILAN ,  italy  ") == "italy"


def test_unknown_location_resolves_to_none():
    assert resolve_country("Europe") is None
    assert resolve_country("European Union") is None
    assert resolve_country("Remote") is None
    assert resolve_country("") is None


def test_tier3_keeps_eu_and_eea_countries():
    for location in ("Berlin, Germany", "Madrid, Spain", "Oslo, Norway",
                     "Reykjavik, Iceland", "Vaduz, Liechtenstein"):
        assert is_in_scope(location, TIER3_ALLOWED_COUNTRIES) is True


def test_tier3_discards_countries_other_tiers_own():
    for location in ("Milan, Italy", "London, United Kingdom",
                     "Zurich, Switzerland", "San Marino"):
        assert is_in_scope(location, TIER3_ALLOWED_COUNTRIES) is False


def test_tier3_discards_non_eea_europe():
    for location in ("Istanbul, Turkey", "Belgrade, Serbia", "Kyiv, Ukraine"):
        assert is_in_scope(location, TIER3_ALLOWED_COUNTRIES) is False


def test_tier3_keeps_unresolvable_location_for_the_verifier():
    for location in ("Europe", "European Union", "Remote", "", "N/A"):
        assert is_in_scope(location, TIER3_ALLOWED_COUNTRIES) is True


def test_no_allowed_set_keeps_everything():
    assert is_in_scope("Istanbul, Turkey", None) is True
    assert is_in_scope("Milan, Italy", None) is True
