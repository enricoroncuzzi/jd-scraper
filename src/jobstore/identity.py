"""Pure identity helpers: when two sightings are the same job.

Rule 1 (same source, same source job id) is enforced by the sightings unique
constraint. Rule 2 (different sources) uses the normalized company, normalized
title and country computed here. When a rule is not clearly met the jobs stay
separate: a duplicate is acceptable, a wrong merge is not.
"""
import re
import unicodedata
from urllib.parse import urlsplit

# Whole tokens removed from the END of a company name (repeatedly), so
# "Acme Group GmbH & Co. KG" normalizes to "acme group".
_LEGAL_SUFFIX_TOKENS = frozenset({
    "srl", "srls", "spa", "sapa", "snc", "sas", "scarl", "gmbh", "ag", "kg", "ug", "ohg",
    "ltd", "limited", "llc", "llp", "lp", "inc", "incorporated", "corp", "corporation",
    "co", "company", "plc", "sa", "sl", "slu", "nv", "bv", "ab", "oy", "oyj", "as", "asa",
    "aps", "sarl", "sro", "zoo", "kft", "pty", "ehf",
})

_DROPPED_INSIDE_WORDS = re.compile(r"[.'’`]")
_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_LINKEDIN_ID = re.compile(r"(\d{5,})$")

# Hosts that are boards or aggregators, never the employer's own page.
_BOARD_HOST_SUFFIXES = (
    "linkedin.com", "himalayas.app", "freehire.me", "whatjobs.com", "jobgether.com",
    "indeed.com", "glassdoor.com", "adzuna.com", "adzuna.co.uk", "reed.co.uk",
    "remoteok.com", "weworkremotely.com", "workingnomads.com", "jobicy.com",
    "remotive.com", "arbeitnow.com", "themuse.com", "ziprecruiter.com", "talent.com",
    "jooble.org", "careerjet.com", "jobrapido.com", "trovolavoro.it", "infojobs.net",
    "monster.com", "wellfound.com", "workable.com/jobs", "jobs.workable.com", "jobleads.com",
)


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def _tokens(text: str) -> list[str]:
    folded = _DROPPED_INSIDE_WORDS.sub("", _fold(text))
    return [token for token in _NON_ALNUM.split(folded) if token]


def normalize_title(title: str) -> str:
    """Lowercase, accents folded, punctuation removed, whitespace collapsed."""
    return " ".join(_tokens(title))


def normalize_company(name: str) -> str:
    """normalize_title plus legal suffixes removed from the end. A name made
    only of suffix tokens (a company called "AB") keeps them."""
    tokens = _tokens(name)
    stripped = list(tokens)
    while len(stripped) > 1 and stripped[-1] in _LEGAL_SUFFIX_TOKENS:
        stripped.pop()
    return " ".join(stripped)


def linkedin_job_id(url: str) -> str | None:
    """The numeric LinkedIn job id from a /jobs/view/ URL, or None."""
    parts = urlsplit(url.strip())
    if "linkedin." not in parts.netloc.lower():
        return None
    last = parts.path.rstrip("/").rsplit("/", 1)[-1]
    match = _LINKEDIN_ID.search(last)
    return match.group(1) if match else None


def is_company_page(url: str) -> bool:
    """True when `url` is not a known board or aggregator host."""
    host = urlsplit(url.strip()).netloc.lower().split(":")[0]
    if not host:
        return False
    for suffix in _BOARD_HOST_SUFFIXES:
        bare = suffix.split("/")[0]
        if host == bare or host.endswith("." + bare):
            return False
    return True


def choose_canonical(urls: list[str]) -> str:
    """The company's own page when any URL is one, else the first URL."""
    for url in urls:
        if url and is_company_page(url):
            return url
    return next(url for url in urls if url)
