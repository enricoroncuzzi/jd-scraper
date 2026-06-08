from langdetect import detect, DetectorFactory
from src.models import JobOffer

DetectorFactory.seed = 0

_ALLOWED_LANGUAGES = {"en", "es", "it"}
_MIN_TEXT_LENGTH = 40


def filter_by_language(offers: list[JobOffer]) -> list[JobOffer]:
    return [o for o in offers if _passes_language_check(o)]


def _passes_language_check(offer: JobOffer) -> bool:
    text = offer.description.strip()
    if len(text) < _MIN_TEXT_LENGTH:
        return True
    try:
        return detect(text) in _ALLOWED_LANGUAGES
    except Exception:
        return True
