"""Language check on the description, same rule as src/language_filter.py:
English, Spanish and Italian pass; very short text and text the detector cannot
classify pass too. Also reports the detected code for jobs.language."""
from langdetect import detect

from src.language_filter import _ALLOWED_LANGUAGES, _MIN_TEXT_LENGTH


def classify_language(text: str) -> tuple[str | None, bool]:
    """(detected code or None, passes)."""
    text = text.strip()
    if len(text) < _MIN_TEXT_LENGTH:
        return None, True
    try:
        language = detect(text)
    except Exception:  # noqa: BLE001 - langdetect raises on undetectable text
        return None, True
    return language, language in _ALLOWED_LANGUAGES
