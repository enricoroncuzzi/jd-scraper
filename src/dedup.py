import hashlib
import os
from src.models import JobOffer


def _hash(link: str) -> str:
    return hashlib.md5(link.encode()).hexdigest()


def _load_seen(log_path: str) -> set[str]:
    if not os.path.exists(log_path):
        return set()
    with open(log_path) as f:
        return {line.strip() for line in f if line.strip()}


def filter_new(offers: list[JobOffer], log_path: str) -> list[JobOffer]:
    seen = _load_seen(log_path)
    return [o for o in offers if _hash(o.link) not in seen]


def mark_seen(offers: list[JobOffer], log_path: str) -> None:
    """Record offers as HANDLED, not merely fetched.

    An entry here is permanent: `filter_new` will never return that link
    again, so an offer the pipeline fetched but did not finish processing
    must be left out and deferred instead (see `src/retry_queue.py`).
    """
    os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
    with open(log_path, "a") as f:
        for offer in offers:
            f.write(_hash(offer.link) + "\n")
