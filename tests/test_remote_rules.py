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

from src.remote_rules import (
    RULE_PREFIX, _scan_hybrid, classify, confident_hybrid_phrase,
    confident_remote_phrase, occasional_presence_phrase, regular_presence_phrase,
    work_mode_hybrid_phrase,
)
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


def test_classify_accepts_the_guard_switches():
    result = classify("This is a hybrid role: three days a week in the office.",
                      title="AI Engineer", occasional_guard=False, cloud_hybrid_guard=False)
    assert result is not None and result.verdict == REJECTED


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

FILLER = ("We build retrieval augmented generation pipelines with Python, PyTorch and FastAPI, "
          "and we ship them to production with Docker and GitHub Actions CI/CD. ") * 2

# Failing-first proof: the first seven are rejected on main and the guard fixes them.
OCCASIONAL_TEXTS = [
    "Occasional on-site presence at the Milan office during the quarter.",
    "2 days a week in the office during the 3-week onboarding period.",
    "Quarterly in-office days for the whole team.",
    "2 days a year in the office.",
    "On-site presence is required for our team sprints.",
    "In-office days for team sprints in London.",
    "Canonical has very few office-based roles.",
    # Already pass without the guard; kept as documentation of the class.
    "Teams meet two to four times yearly in person for team sprints.",
    "Expenses-paid trips to HQ for team meetups twice a year.",
    "Occasional travel for client workshops and team meetings.",
]
REGULAR_HYBRID_TEXTS = [
    "This is a hybrid role: three days a week in the office in Milan.",
    "We work hybrid, 2 days on site each week in our Rome office.",
    "3 days a week in the office, with occasional overtime.",
    "3 days a week in the office, plus a quarterly bonus.",
    "Hybrid: 2 days a week in the office. We also run quarterly team offsites.",
]
# A hybrid-looking phrase suppressed by the occasional guard must reach the
# model, never the silent branch: not_stated would hide a real hybrid job.
MIXED_HYBRID_TEXTS = [
    "Two days a week in the office (Tuesday and Thursday), plus a quarterly team offsite.",
    "2 days a week in the office during the 3-week onboarding period.",
    "2 days a year in the office.",
]
# Remote plus a suppressed phrase must not be auto-confirmed (confirmed is the
# only verdict that gets auto-tailored): the model decides, as it does today.
REMOTE_PLUS_OCCASIONAL_TEXTS = [
    "We are a remote-first company. Three days a week in the office in Milan, plus a team "
    "offsite each quarter.",
    "Remote-first culture. Hybrid schedule: in-office days on Tuesday and Thursday, with "
    "quarterly team events.",
    "Acme is a fully remote company. There is occasional on-site presence at the Milan "
    "office during onboarding, and a few team events during the year, but the role is done "
    "from home.",
]


def _verdict_of(text, **kwargs):
    result = classify(text, title="AI Engineer", **kwargs)
    return result.verdict if result else None


def test_occasional_presence_never_decides_hybrid():
    for text in OCCASIONAL_TEXTS:
        assert _verdict_of(FILLER + text) != REJECTED, text


def test_regular_office_days_are_still_hybrid():
    for text in REGULAR_HYBRID_TEXTS:
        assert _verdict_of(FILLER + text) == REJECTED, text


def test_a_suppressed_hybrid_phrase_goes_to_the_model_not_not_stated():
    for text in MIXED_HYBRID_TEXTS:
        assert _verdict_of(FILLER + text) is None, text


def test_remote_plus_a_suppressed_phrase_is_not_auto_confirmed():
    for text in REMOTE_PLUS_OCCASIONAL_TEXTS:
        assert _verdict_of(FILLER + text) is None, text


def test_occasional_presence_phrase_reports_the_marker():
    assert occasional_presence_phrase("Occasional travel for client workshops.") == "Occasional travel"
    # The sprint alternative is the bare word: that is what the function returns.
    assert occasional_presence_phrase("Mandatory attendance at quarterly team sprints in London") \
        == "sprints"
    assert occasional_presence_phrase("You will work from our Rome office every day.") is None


def test_a_few_office_days_is_not_the_very_few_idiom():
    # "a few in-office days a week" is a regular hybrid policy; only the
    # "very few <office word>" idiom says the opposite.
    assert _verdict_of(FILLER + "We are a remote-first company. Expect a few in-office "
                                 "days per week.") is None
    assert _verdict_of(FILLER + "Expect a few in-office days per week.") == REJECTED


def test_the_very_few_idiom_follows_the_occasional_switch():
    canonical = FILLER + "Canonical has very few office-based roles."
    assert _verdict_of(canonical) != REJECTED
    assert _verdict_of(canonical, occasional_guard=False) == REJECTED


def test_a_suppressed_phrase_is_reported_separately_from_a_confident_one():
    confident, suppressed = _scan_hybrid(
        "2 days a week in the office during onboarding.", occasional_guard=True,
        cloud_hybrid_guard=True)
    assert confident is None and suppressed is not None

CLOUD_HYBRID_TEXTS = [
    "You will operate and secure hybrid environments for our customers, spanning private cloud and "
    "public cloud workloads with Terraform and Kubernetes. You will join a team of platform engineers "
    "delivering observability, secrets management and CI/CD pipelines for regulated clients.",
    "Our client runs a multi-cloud and hybrid environments estate. You will design automated "
    "provisioning with Ansible and Terraform, improve monitoring and support the migration of "
    "legacy workloads. A background in infrastructure engineering and scripting is what we ask for.",
]
WORKPLACE_HYBRID_ENVIRONMENT_TEXTS = [
    "We provide a flexible hybrid working environment for all staff.",
    "Join our hybrid work environment in Milan.",
    "We work in a hybrid environment, as we value flexibility.",
]


def test_hybrid_technology_wording_is_not_a_work_mode():
    for text in CLOUD_HYBRID_TEXTS:
        assert _verdict_of(FILLER + text) == NOT_STATED, text


def test_a_hybrid_working_environment_is_still_hybrid():
    for text in WORKPLACE_HYBRID_ENVIRONMENT_TEXTS:
        assert _verdict_of(FILLER + text) == REJECTED, text


def test_a_hybrid_work_sentence_next_to_cloud_wording_still_rejects():
    text = ("You will operate secure hybrid environments for our customers, spanning private "
            "cloud and public cloud workloads. This role is hybrid: 3 days a week in the office.")
    assert _verdict_of(FILLER + text) == REJECTED


def test_confident_remote_phrase_reads_only_explicit_full_remote_forms():
    assert confident_remote_phrase("Junior AI Engineer - Pharma (Full remote)") == "Full remote"
    assert confident_remote_phrase("This role is 100% remote.") == "100% remote"
    assert confident_remote_phrase("This is not a fully remote role.") is None
    # An option is not a statement, exactly like classify's own remote scan.
    assert confident_remote_phrase("Fully remote or hybrid, you choose.") is None
    # Wider remote wording ("remote-first", "work from anywhere") does not
    # override a source hint; only the explicit full-remote forms do.
    assert confident_remote_phrase("We are a remote-first company.") is None


def test_regular_presence_phrase_reads_every_weekly_cadence():
    cadences = [
        "2 days in office per week, plus team sprints",
        "2 days/week in the office and sprints",
        "Hybrid: 2 office days a week and sprints",
        "Team day in office, two days in the week; sprints",
        "two days weekly in our office",
        "3x a week at the Milan site",
    ]
    for reason in cadences:
        assert regular_presence_phrase(reason) is not None, reason
    for occasional in ["Onsite team events in Warsaw and global team offsites once a year.",
                       "Teams meet two to four times yearly in person for team sprints.",
                       "Team offsites once a quarter in Berlin",
                       "occasional travel for client workshops"]:
        assert regular_presence_phrase(occasional) is None, occasional


def test_work_mode_hybrid_phrase_separates_work_from_technology_and_options():
    assert work_mode_hybrid_phrase("Hybrid role. Two days weekly at our Milan site.") == "Hybrid"
    assert work_mode_hybrid_phrase("We work ibrido in Milan.") == "ibrido"
    assert work_mode_hybrid_phrase("This is not a hybrid role.") is None
    assert work_mode_hybrid_phrase("Fully remote or hybrid, you choose.") is None
    assert work_mode_hybrid_phrase("We run secure hybrid environments for clients.") is None
