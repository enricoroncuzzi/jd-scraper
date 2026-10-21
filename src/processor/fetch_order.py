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
