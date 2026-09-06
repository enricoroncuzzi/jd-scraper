"""Resolve an offer's country from its LinkedIn location string, and decide
whether it falls inside a tier's geographic scope.

Tier 3 issues a single search against the location "Europe", which returns far
more than the EU. This module narrows that result set. The asymmetry below is
deliberate: a location naming a country we recognise but do not want is
discarded, while a location we cannot resolve at all - "Europe", "Remote", an
empty string - is KEPT and left to the verification stage, which is what
actually decides whether a role can be held from Italy. Dropping an offer
because its location did not parse would silently lose exactly the
Europe-wide remote roles this tier exists to find.
"""

# Canonical names are lowercase. Aliases map a surface form onto its canonical
# name; anything not listed resolves to itself when it is a known country.
_COUNTRY_ALIASES = {
    "czech republic": "czechia",
    "uk": "united kingdom",
    "great britain": "united kingdom",
    "england": "united kingdom",
    "scotland": "united kingdom",
    "wales": "united kingdom",
    "northern ireland": "united kingdom",
    "holland": "netherlands",
    "the netherlands": "netherlands",
    "republic of ireland": "ireland",
    "republic of san marino": "san marino",
    "swiss confederation": "switzerland",
    "slovak republic": "slovakia",
    "united states of america": "united states",
    "usa": "united states",
    "us": "united states",
}

_EU_27 = frozenset({
    "austria", "belgium", "bulgaria", "croatia", "cyprus", "czechia", "denmark",
    "estonia", "finland", "france", "germany", "greece", "hungary", "ireland",
    "italy", "latvia", "lithuania", "luxembourg", "malta", "netherlands",
    "poland", "portugal", "romania", "slovakia", "slovenia", "spain", "sweden",
})

_NON_EU_EEA = frozenset({"norway", "iceland", "liechtenstein"})

# Tier 3 is the EU plus the non-EU EEA states, minus every country another tier
# already owns: Italy (tier 1), Switzerland and San Marino (tier 2), the United
# Kingdom (tier 4).
TIER3_ALLOWED_COUNTRIES = frozenset(
    (_EU_27 | _NON_EU_EEA) - {"italy", "united kingdom", "switzerland", "san marino"}
)

# Countries we can positively recognise. A location resolving to one of these
# but outside the allowed set is discarded; anything else is kept.
_KNOWN_COUNTRIES = frozenset(
    _EU_27
    | _NON_EU_EEA
    | {
        "united kingdom", "switzerland", "san marino", "andorra", "monaco",
        "albania", "belarus", "bosnia and herzegovina", "kosovo", "moldova",
        "montenegro", "north macedonia", "russia", "serbia", "turkey", "ukraine",
        "united states", "canada", "india", "australia", "israel",
    }
)


def resolve_country(location: str) -> str | None:
    """Return the canonical lowercase country named by a LinkedIn location
    string, or None when it names no country we recognise.

    LinkedIn writes locations as "City, Region, Country", "City, Country", or a
    bare country, so the country is the last comma-separated component. Some
    listings instead carry "Europe", "Remote", or an empty value; those resolve
    to None by design.
    """
    if not location:
        return None
    tail = location.split(",")[-1].strip().lower()
    if not tail:
        return None
    canonical = _COUNTRY_ALIASES.get(tail, tail)
    return canonical if canonical in _KNOWN_COUNTRIES else None


def is_in_scope(location: str, allowed: frozenset[str] | None) -> bool:
    """True when this offer belongs to a tier whose scope is `allowed`.

    `allowed` of None means the tier does no geographic narrowing at all, which
    is the case for every tier except tier 3.
    """
    if allowed is None:
        return True
    country = resolve_country(location)
    if country is None:
        return True
    return country in allowed
