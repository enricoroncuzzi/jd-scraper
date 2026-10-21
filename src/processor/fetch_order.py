"""Which waiting description is worth fetching next (spec: smart fetch order).

The queue used to be served strictly oldest first. Two signals stored on the job
before any fetch predict a good outcome far better than arrival order does:
a non-senior AI/ML/DS title (13.2% of fetched descriptions end at score >= 8,
against 0.6% for other titles) and a search card whose location is exactly a
country name (70% end remote-confirmed or advised, against 4.2%). Both were
measured in data/jdscraper-missed-offers-audit and
data/jdscraper-workplace-filter-scout. Neither is safe as a skip, so they only
order the queue.

This module holds the ranking as plain data: a per-tier ordered list of classes
(`TierOrder`), the keywords that decide the title kind, and the quota that keeps
a share of the picks on the plain oldest-first order. `src/processor/describe.py`
turns a job's rank into a sort key; `src/processor/cheap.py` writes the rank when
it assigns the tier.
"""
from dataclasses import dataclass, field

from src.processor.titles import title_hits_exclude
from src.tier_scope import is_country_only

TITLE_KINDS = ("ai", "other")
LOCATION_KINDS = ("country_only", "other")


@dataclass(frozen=True)
class FetchOrderClass:
    """One rank in a tier's class order. `location` None means "any location"."""
    name: str
    title: str
    location: str | None = None


@dataclass(frozen=True)
class TierOrder:
    """A tier's class order: index in `classes` is the stored rank, 0 = best."""
    classes: tuple[FetchOrderClass, ...]

    def name(self, rank: int) -> str:
        """The class name for a stored rank; out-of-range ranks keep their number."""
        if 0 <= rank < len(self.classes):
            return self.classes[rank].name
        return f"class{rank}"

    def classify(self, title: str, location_raw: str,
                 ai_keywords: tuple[str, ...], senior_keywords: tuple[str, ...]) -> int:
        """The rank of a job with this title and card location in this tier."""
        kind = title_kind(title, ai_keywords, senior_keywords)
        country = is_country_only(location_raw)
        for rank, cls in enumerate(self.classes):
            if cls.title != kind:
                continue
            if cls.location is None:
                return rank
            if (cls.location == "country_only") is country:
                return rank
        raise RuntimeError(f"no class covers title={kind!r} country_only={country!r}")


@dataclass(frozen=True)
class FetchOrder:
    """The whole fetch-order policy: keywords, class orders, the floor share."""
    enabled: bool
    quota_one_in: int
    backfill_per_tick: int
    ai_title_keywords: tuple[str, ...]
    senior_title_keywords: tuple[str, ...]
    default: TierOrder
    by_tier: dict[int, TierOrder] = field(default_factory=dict)

    def order_for(self, tier: int) -> TierOrder:
        """The class order for `tier` (the default for any tier without its own)."""
        return self.by_tier.get(tier, self.default)

    def classify(self, tier: int, title: str, location_raw: str) -> int:
        order = self.order_for(tier)
        return order.classify(title, location_raw, self.ai_title_keywords,
                              self.senior_title_keywords)

    def name(self, tier: int, rank: int | None) -> str:
        """How status prints a stored rank."""
        return "unclassified" if rank is None else self.order_for(tier).name(rank)

    def quota_turn(self, picks_done: int) -> bool:
        """True when this pick must be the plain oldest-first (floor) pick.

        One pick in `quota_one_in`, per tier, keeps today's order and the
        60-hour floor; every other pick prefers the best class. With the order
        disabled every pick is a floor pick, i.e. exactly today's behaviour.
        """
        return (not self.enabled) or picks_done % self.quota_one_in == 0


def title_kind(title: str, ai_keywords: tuple[str, ...],
               senior_keywords: tuple[str, ...]) -> str:
    """"ai" for a non-senior AI/ML/DS title, "other" for everything else.

    Whole-word matching, shared with the tier prefilter through
    `title_hits_exclude`, so "Training Coordinator" is not an AI title and
    "Senior AI Engineer" is demoted by its seniority word.
    """
    if title_hits_exclude(title, ai_keywords) is None:
        return "other"
    return "other" if title_hits_exclude(title, senior_keywords) is not None else "ai"


# The shipped class lists and keywords. DEFAULT_FETCH_ORDER is what a config file
# without a `description_order` block loads: disabled, so an older config file (or
# a missing key) can never change the queue order silently.
DEFAULT_AI_TITLE_KEYWORDS = (
    "AI", "GenAI", "generative AI", "artificial intelligence", "machine learning",
    "ML", "MLOps", "deep learning", "LLM", "LLMs", "NLP", "data scientist",
    "data scientists", "data science", "computer vision",
)
DEFAULT_SENIOR_TITLE_KEYWORDS = (
    "Senior", "Sr", "Lead", "Principal", "Staff", "Head", "Director", "VP",
    "Vice President", "Chief", "Manager",
)
DEFAULT_CLASSES = (
    FetchOrderClass("ai_country", "ai", "country_only"),
    FetchOrderClass("ai_city", "ai", "other"),
    FetchOrderClass("other_country", "other", "country_only"),
    FetchOrderClass("other_city", "other", "other"),
)

DEFAULT_FETCH_ORDER = FetchOrder(
    enabled=False,
    quota_one_in=4,
    backfill_per_tick=500,
    ai_title_keywords=DEFAULT_AI_TITLE_KEYWORDS,
    senior_title_keywords=DEFAULT_SENIOR_TITLE_KEYWORDS,
    default=TierOrder(classes=DEFAULT_CLASSES),
    by_tier={},
)


def _positive_int(data: dict, key: str, default: int) -> int:
    value = data.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"description_order.{key} must be an integer >= 1, got {value!r}")
    return value


def _keyword_tuple(data: dict, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = data.get(key, list(default))
    if (not isinstance(value, list) or not value
            or any(not isinstance(word, str) or not word.strip() for word in value)):
        raise ValueError(f"description_order.{key} must be a non-empty list of non-empty strings")
    return tuple(value)


def _tier_order(spec, where: str) -> TierOrder:
    """Validate one class list and turn it into a `TierOrder`."""
    if not isinstance(spec, list):
        raise ValueError(f"{where} must be a list of classes")
    classes: list[FetchOrderClass] = []
    names: set[str] = set()
    for index, entry in enumerate(spec):
        if not isinstance(entry, dict):
            raise ValueError(f"{where}[{index}] must be an object with a name and a title")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{where}[{index}].name must be a non-empty string")
        if name in names:
            raise ValueError(f"{where} has a duplicate class name: {name!r}")
        names.add(name)
        title = entry.get("title")
        if title not in TITLE_KINDS:
            raise ValueError(f'{where}[{index}].title must be "ai" or "other", got {title!r}')
        location = entry.get("location")
        if "location" in entry and location not in LOCATION_KINDS:
            raise ValueError(f'{where}[{index}].location must be "country_only" or "other", '
                             f"got {location!r}")
        classes.append(FetchOrderClass(name, title, location))
    uses_location = {cls.location is not None for cls in classes}
    if len(uses_location) > 1:
        raise ValueError(f"{where} must use one key set (title, or title and location)")
    with_location = uses_location == {True}
    covered = {(cls.title, cls.location if with_location else None) for cls in classes}
    expected = {(title, location if with_location else None)
                for title in TITLE_KINDS
                for location in (LOCATION_KINDS if with_location else (None,))}
    if covered != expected or len(classes) != len(expected):
        raise ValueError(f"{where} must cover every combination of its keys exactly once")
    return TierOrder(classes=tuple(classes))


def load_fetch_order(data: dict | None) -> FetchOrder:
    """Read and validate the `description_order` block.

    A missing block (an older config file) loads `DEFAULT_FETCH_ORDER`, which is
    disabled. Every validation failure raises ValueError naming the key, so a
    typo stops the service loudly instead of silently reordering the queue.
    """
    if data is None:
        return DEFAULT_FETCH_ORDER
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"description_order.enabled must be a boolean, got {enabled!r}")
    quota_one_in = _positive_int(data, "quota_one_in", DEFAULT_FETCH_ORDER.quota_one_in)
    backfill_per_tick = _positive_int(data, "backfill_per_tick",
                                      DEFAULT_FETCH_ORDER.backfill_per_tick)
    ai_keywords = _keyword_tuple(data, "ai_title_keywords", DEFAULT_AI_TITLE_KEYWORDS)
    senior_keywords = _keyword_tuple(data, "senior_title_keywords", DEFAULT_SENIOR_TITLE_KEYWORDS)
    shared = {word.lower() for word in ai_keywords} & {word.lower() for word in senior_keywords}
    if shared:
        raise ValueError(f"description_order keywords must not appear in both lists: "
                         f"{sorted(shared)[0]!r}")
    classes = data.get("classes")
    if not isinstance(classes, dict) or "default" not in classes:
        raise ValueError('description_order.classes must define a "default" class order')
    by_tier: dict[int, TierOrder] = {}
    for key, spec in classes.items():
        if key == "default":
            continue
        if key not in ("1", "2", "3", "4"):
            raise ValueError(f"description_order.classes key {key!r} must be a tier from 1 to 4")
        by_tier[int(key)] = _tier_order(spec, f'description_order.classes["{key}"]')
    return FetchOrder(enabled=enabled, quota_one_in=quota_one_in,
                      backfill_per_tick=backfill_per_tick,
                      ai_title_keywords=ai_keywords, senior_title_keywords=senior_keywords,
                      default=_tier_order(classes["default"], 'description_order.classes["default"]'),
                      by_tier=by_tier)
