from src.models import JobOffer
from src.language_filter import filter_by_language

EN_TEXT = (
    "We are looking for a talented machine learning engineer to join our growing "
    "team and help us build production AI systems that scale to millions of users."
)
ES_TEXT = (
    "Buscamos un ingeniero de machine learning con experiencia para unirse a "
    "nuestro equipo y construir sistemas de inteligencia artificial en produccion."
)
IT_TEXT = (
    "Cerchiamo un ingegnere di machine learning con esperienza per unirsi al "
    "nostro team e costruire sistemi di intelligenza artificiale in produzione."
)
DE_TEXT = (
    "Wir suchen einen erfahrenen Machine-Learning-Ingenieur, der unserem Team "
    "beitritt und KI-Systeme fuer die Produktion in grossem Massstab entwickelt."
)


def _offer(description: str) -> JobOffer:
    return JobOffer(id=0, title="t", company="c", link="https://li.com/1", description=description)


def test_keeps_english_description():
    result = filter_by_language([_offer(EN_TEXT)])
    assert len(result) == 1


def test_keeps_spanish_description():
    result = filter_by_language([_offer(ES_TEXT)])
    assert len(result) == 1


def test_keeps_italian_description():
    result = filter_by_language([_offer(IT_TEXT)])
    assert len(result) == 1


def test_drops_german_description():
    result = filter_by_language([_offer(DE_TEXT)])
    assert result == []


def test_fails_open_on_short_description():
    result = filter_by_language([_offer("Kurze Stellenbeschreibung.")])
    assert len(result) == 1


def test_fails_open_on_empty_description():
    result = filter_by_language([_offer("")])
    assert len(result) == 1


def test_fails_open_when_detection_raises(monkeypatch):
    from langdetect.lang_detect_exception import LangDetectException

    def raise_detect(text):
        raise LangDetectException(0, "detection failed")

    monkeypatch.setattr("src.language_filter.detect", raise_detect)
    result = filter_by_language([_offer(EN_TEXT)])
    assert len(result) == 1
