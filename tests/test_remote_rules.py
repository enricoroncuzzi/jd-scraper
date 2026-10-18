"""The deterministic work-mode pre-pass (src/remote_rules.py).

The labelled sample is the scout's hand-labelled 181 jobs (2026-10-18), trimmed
here to the raw phrases the scout quoted as evidence: full job descriptions are
deliberately not committed. `remote_labelled_sample.csv` keeps one row per
labelled job with its label (R/H/O/C/P/U) and tier, so these tests check the
property that matters for a rule layer that rejects and confirms without a model:

- it never rejects a job the scout labelled genuinely remote (0 false rejects),
- it never confirms a job labelled hybrid, on-site, a perk, or silent,
- a job labelled remote-but-limited-to-another-country (C) is advised, never
  rejected and never confirmed (D3).
"""
import csv
import os

import pytest

from src.remote_rules import RULE_PREFIX, classify
from src.remote_verdicts import ADVISED, CONFIRMED, NOT_STATED, REJECTED

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "remote_labelled_sample.csv")


def _rows():
    with open(FIXTURE, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _rule(row):
    return classify(row["text"], title=row["title"], location=row["location"],
                    remote_hint=row["remote_hint"] or None,
                    require_italy_eligibility=row["tier"] in ("3", "4"),
                    # The source-hint shortcut has its own test below; keep the
                    # text-rule property independent of it.
                    hints_enabled=False)


def _verdict(row):
    result = _rule(row)
    return result.verdict if result else None


def test_the_rule_layer_never_rejects_a_genuinely_remote_job():
    false_rejects = [row["k"] for row in _rows()
                     if row["label"] == "R" and _verdict(row) == REJECTED]
    assert false_rejects == []


def test_only_a_remote_with_an_eligibility_limit_gets_an_advice():
    for row in _rows():
        if row["label"] == "C":
            assert _verdict(row) in (ADVISED, NOT_STATED, None), row
        elif row["label"] != "R":
            assert _verdict(row) not in (CONFIRMED, ADVISED), row


def test_silent_text_is_never_confirmed():
    silent = [row for row in _rows() if row["label"] == "U"]
    assert silent
    assert all(_verdict(row) not in (CONFIRMED, ADVISED) for row in silent)


def test_hybrid_and_on_site_labels_are_never_confirmed():
    for row in _rows():
        if row["label"] in ("H", "O"):
            assert _verdict(row) not in (CONFIRMED, ADVISED), row


def test_it_still_rejects_the_clear_hybrid_and_on_site_phrases():
    rejected = [row for row in _rows() if row["label"] in ("H", "O") and _verdict(row) == REJECTED]
    assert len(rejected) >= 10


@pytest.mark.parametrize("text,expected", [
    ("This role is fully remote.", CONFIRMED),
    ("We are a remote-first company.", CONFIRMED),
    ("Location: Remote", CONFIRMED),
    ("100% da remoto, nessun obbligo di sede.", CONFIRMED),
    ("Télétravail total, 100 % à distance.", CONFIRMED),
    ("Vollständig remote, arbeiten Sie von überall.", CONFIRMED),
    ("Teletrabajo al 100%.", CONFIRMED),
    ("Offriamo la possibilità di lavoro da remoto.", None),  # a perk: the model decides
    ("We offer smart working and flexible hours.", None),
    ("Hybrid working pattern, 3+ days office attendance required.", REJECTED),
    ("This is a fully on-site role in Milan.", REJECTED),
    ("Modelo híbrido de trabajo, 2 días en la oficina.", REJECTED),
    ("Présentiel, trois jours au bureau.", REJECTED),
    ("Vor Ort in München, keine Remote-Option.", None),  # also a customer site: the model decides
    ("Lavoro in presenza, 5 giorni in ufficio.", REJECTED),
    ("Hybrid search and hybrid cloud infrastructure.", None),  # not a work mode
    ("Remote sensing engineer for satellite imagery.", None),
    ("This is not a fully remote role.", None),
])
def test_confident_phrases_in_five_languages(text, expected):
    result = classify(text, title="AI Engineer", location="Milan, Italy")
    assert (result.verdict if result else None) == expected


@pytest.mark.parametrize("text", [
    "Azienda leader con una forte presenza internazionale nel settore dell'energia.",
    "We are a remote company. Benefits: 25 days of holiday, home office equipment budget.",
    "Lavorerai su un'architettura cloud ibrida basata su Kubernetes.",
    "Wir entwickeln hybride Cloud-Lösungen für den Mittelstand.",
    "Starke Marktpräsenz in der DACH-Region.",
    "Sie betreuen unsere Kunden vor Ort bei der Einführung.",
    "Du arbeitest eng mit dem Team im Büro und remote zusammen.",
    "Remote-first. Perks: 30 days of annual leave, a 500 EUR office allowance.",
])
def test_non_workplace_wording_is_never_rejected_by_rule(text):
    filler = " We build retrieval pipelines in Python and evaluate language models daily." * 3
    result = classify(text + filler, title="AI Engineer", location="Milan, Italy",
                      require_italy_eligibility=True)
    assert result is None or result.verdict != REJECTED


def test_a_description_with_no_work_mode_word_is_not_stated_without_a_model():
    description = ("We build retrieval augmented generation pipelines and evaluate large language "
                   "models. You will ship features with Python, PyTorch and FastAPI every day, "
                   "working with a small product team in our Milan headquarters.")
    result = classify(description, title="AI Engineer", location="Milan, Italy")
    assert result is not None and result.verdict == NOT_STATED
    assert result.reason.startswith(RULE_PREFIX)


def test_a_short_fragment_is_left_to_the_model_instead_of_being_written_off():
    assert classify("AI Engineer at Acme.", title="AI Engineer", location="Milan, Italy") is None


def test_an_explicit_country_limit_is_advised_not_rejected_in_tiers_three_and_four():
    result = classify("This is a fully remote role. Remote (UK) only.",
                      title="AI Engineer", location="London, United Kingdom",
                      require_italy_eligibility=True)
    assert result.verdict == ADVISED
    assert result.advice == "remote, limited to the UK - check eligibility"


@pytest.mark.parametrize("text", [
    "This is a fully remote role. Remote - Italy.",
    "This is a fully remote role in Italy.",
    "This is a fully remote role. Location: Remote (Italy).",
])
def test_an_explicit_italy_scope_is_confirmed(text):
    result = classify(text, title="AI Engineer", location="Berlin, Germany",
                      require_italy_eligibility=True)
    assert (result.verdict, result.advice) == (CONFIRMED, "")


# Residency wording is about the company, its team or its clients as often as it
# is a condition on the candidate, so the rules never read it: in tiers 3 and 4
# every one of these goes to the model, which answers eligibility itself.
@pytest.mark.parametrize("text", [
    "This is a fully remote role open to candidates across the EU. We are one of the only AI "
    "companies in the UK with a medical licence.",
    "This is a fully remote role. Applicants from the EU are welcome. Our clients include the "
    "only Spain-based neobank.",
    "This is a fully remote role. We are proud corporate citizens in the UK.",
    "This is a fully remote role. Candidates must live in Germany. We are a leading AI "
    "company in Europe.",
    "This is a fully remote role. UK citizens only. We serve clients across Europe.",
    "This is a fully remote role, open to candidates living in Spain only. Our offices are "
    "in Italy.",
    "This is a fully remote role open to candidates across Europe. Our team members live in "
    "Germany, Spain and Portugal.",
    "This is a fully remote role. Most of us live in Berlin, Germany, and we hire across the EU.",
    "This is a fully remote role open to candidates across Europe. We build an app that "
    "helps people living in Germany find a doctor.",
    "This is a fully remote role open to candidates across Europe. Our company is based in "
    "Berlin, Germany.",
    "This is a fully remote role. You must reside in Germany, not Italy.",
    "This is a fully remote role. Our Vice Presidents in Germany lead the team.",
    "This is a fully remote role. You must reside in Germany or Italy.",
    "This is a fully remote role. You must reside in one of:\nItaly\nGermany",
    "This is a fully remote role across our Europe-wide team. You must reside in one of:\n"
    "Germany\nSpain\nWe ship tooling for engineers.",
    "This is a fully remote role. You must be eligible to work in one of:\nthe EU\nthe EEA\n"
    "About us\nOur HQ is in Berlin, Germany.",
    "This is a fully remote role. You must be eligible to work in the EU. "
    "Our HQ is in Berlin, Germany.",
    "This is a fully remote role, open across Europe. We respect your right to work-life "
    "balance; our office is in Berlin, Germany.",
    "This is a fully remote role. Open to candidates living in Italy only.",
    "Fully remote.",
])
def test_residency_wording_leaves_eligibility_to_the_model_in_tiers_three_and_four(text):
    assert classify(text, title="AI Engineer", location="Berlin, Germany",
                    require_italy_eligibility=True) is None


def test_tier_one_needs_no_eligibility_advice():
    result = classify("Fully remote.", title="AI Engineer", location="Milan, Italy",
                      require_italy_eligibility=False)
    assert result.verdict == CONFIRMED and result.advice == ""


def test_the_source_hint_rejects_hybrid_and_on_site_and_nothing_else():
    for hint in ("hybrid", "onsite"):
        result = classify("A description with no work-mode words at all. " * 10,
                          title="AI Engineer", location="Milan, Italy", remote_hint=hint)
        assert result.verdict == REJECTED and "source work mode" in result.reason
    # The confirm shortcuts are not part of this switch: 87.3% and 66.7% agreement.
    # The silent text is not stated, but it is emphatically not confirmed.
    silent = classify("A description with no work-mode words at all. " * 10,
                      remote_hint="remote_worldwide")
    assert silent.verdict == NOT_STATED


def test_the_hint_shortcut_can_be_switched_off():
    result = classify("A description with no work-mode words at all. " * 10,
                      remote_hint="onsite", hints_enabled=False)
    assert result.verdict == NOT_STATED
