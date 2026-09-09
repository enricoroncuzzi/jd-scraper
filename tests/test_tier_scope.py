import pytest

from src.tier_scope import (
    TIER3_ALLOWED_COUNTRIES,
    is_in_scope,
    resolve_allowed_countries,
    resolve_country,
)


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


def test_resolves_observed_us_and_european_locations():
    # Exact strings observed on 2026-09-08/09 tier 2 digests plus the Swiss
    # metro-area forms that must stay resolvable/keepable.
    assert resolve_country("El Segundo, CA") == "united states"
    assert resolve_country("Irvine, CA") == "united states"
    assert resolve_country("Los Angeles Metropolitan Area") is None
    assert resolve_country("San Marino, San Marino") == "san marino"
    assert resolve_country("Zurich, Zurich, Switzerland") == "switzerland"
    assert resolve_country("Basel Metropolitan Area") is None
    assert resolve_country("Lausanne Metropolitan Area") is None


def test_tier2_scope_over_observed_strings():
    allowed = frozenset({"switzerland", "san marino"})
    assert is_in_scope("El Segundo, CA", allowed) is False
    assert is_in_scope("Irvine, CA", allowed) is False
    # Unresolvable strings stay kept by design (see tier_scope module docstring),
    # which is why the US metro-area form below is the residual gap.
    assert is_in_scope("Los Angeles Metropolitan Area", allowed) is True
    assert is_in_scope("San Marino, San Marino", allowed) is True
    assert is_in_scope("Zurich, Zurich, Switzerland", allowed) is True
    assert is_in_scope("Basel Metropolitan Area", allowed) is True
    assert is_in_scope("Lausanne Metropolitan Area", allowed) is True


def test_resolve_allowed_countries_returns_none_when_unconfigured():
    assert resolve_allowed_countries(None) is None
    assert resolve_allowed_countries([]) is None


def test_resolve_allowed_countries_canonicalizes_names():
    assert resolve_allowed_countries(["Switzerland", "San Marino"]) == frozenset({"switzerland", "san marino"})
    assert resolve_allowed_countries(["Czech Republic"]) == frozenset({"czechia"})


def test_resolve_allowed_countries_rejects_unknown_names():
    with pytest.raises(ValueError):
        resolve_allowed_countries(["Narnia"])


def test_two_letter_tails_colliding_with_iso2_codes_stay_unresolvable():
    # "DE"/"MT"/"MD"/"ME"/"AL"/"IL"/"IN" are ISO-3166 alpha-2 country codes and
    # "NE"/"AR" are Swiss canton abbreviations, all of which also happen to be
    # US state postal codes. Mislabelling them "united states" would drop the
    # offer from a European scope before its description is ever fetched.
    for location in ("Berlin, DE", "Valletta, MT", "Chisinau, MD",
                     "Podgorica, ME", "Tirana, AL", "Tel Aviv, IL",
                     "Bengaluru, IN", "Neuchatel, NE", "Herisau, AR"):
        assert resolve_country(location) is None
        assert is_in_scope(location, TIER3_ALLOWED_COUNTRIES) is True
        assert is_in_scope(location, frozenset({"switzerland", "san marino"})) is True


def test_unambiguous_us_state_tails_still_resolve():
    for location in ("El Segundo, CA", "Austin, TX", "Seattle, WA",
                     "Washington, DC", "Boston, MA"):
        assert resolve_country(location) == "united states"
