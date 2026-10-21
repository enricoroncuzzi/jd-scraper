"""Whole-word keyword matching on job titles.

Shared by the tier prefilter (`src/processor/cheap.py`, which drops a title
carrying a tier's exclude keyword before any fetch) and by the description fetch
order (`src/processor/fetch_order.py`, which ranks titles by class). It lives
here rather than in `cheap.py` so the fetch order can import it without pulling
in the processor config, which imports the fetch order.
"""
import re


def title_hits_exclude(title: str, keywords: tuple[str, ...]) -> str | None:
    """The first keyword present in the title as a whole word or phrase."""
    for keyword in keywords:
        if re.search(rf"(?<![\w+]){re.escape(keyword)}(?![\w+])", title, re.IGNORECASE):
            return keyword
    return None
