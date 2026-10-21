"""Regression suite from the 2026-10-20 missed-offers audit (1.3 A, C, D, E).

Each excerpt is written from the audit's quotes; full descriptions are not
committed. The rule layer must never reject an audit case, the source's remote
hint must win over occasional-presence evidence, an explicit "(Full remote)"
must beat a structured hybrid hint, cloud "hybrid environments" must not reject,
and a genuine hybrid must still reject.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from src.jobstore.sightings import CardInput, record_sighting
from src.processor.cheap import check_one
from src.processor.config import load_processor_config
from src.processor.llm_stages import LlmKeys
from src.processor.remote import apply_deterministic_rules, run_remote_batches
from src.remote_rules import classify
from src.remote_verdicts import ADVISED, NOT_STATED, REJECTED

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
FILLER = ("We build retrieval augmented generation pipelines with Python, PyTorch and FastAPI, "
          "and we ship them to production with Docker and GitHub Actions CI/CD. ") * 2

SIBILL = ("sibill is hiring an AI Engineer. Full remote (con residenza in Italia). You will build "
          "LLM agents and retrieval pipelines in Python every day. You will come to Milano for "
          "some weeks during onboarding, then work from anywhere in Italy.")
KALEPA = ("Kalepa is a fully remote company. Fully remote work from Europe, about one year of "
          "experience. We get together for onsite team events in Warsaw and global team offsites "
          "once a year. You will write Python services with our data platform team.")
CANONICAL = ("Canonical is a remote-first company with very few office-based roles. Teams meet two "
             "to four times yearly in person for team sprints and internal events. You will work on "
             "Kubernetes and Python tooling for data and AI workloads.")
JOBGETHER = ("This role is remote, based in Italy. You will build and deploy Python applications for "
             "clients. The position requires occasional travel for client workshops and team "
             "meetings. We offer a competitive salary and a fully distributed team of engineers.")
SHIPPYPRO = ("ShippyPro is hiring a Data Platform Engineer. Work from anywhere in Italy. The team "
             "meets in person for expenses-paid trips to our HQ for team meetups twice a year. You "
             "will own our Airflow and dbt pipelines and help the data team scale them.")
AFFIDEA = ("We are looking for a junior engineer to join the digital innovation team. You will "
           "prototype LLM applications with LangChain and HuggingFace models, support the data team "
           "and document what you build for the wider group of clinics. A master's degree and some "
           "Python experience are what we ask for.")
AMBIT = ("Ambit is hiring a Junior AI Engineer for the pharma industry. Full remote. You will build "
         "RAG pipelines with Python and LangChain for clinical documentation. We look for a recent "
         "graduate with some experience and a lot of curiosity.")
CLOUD_NOVA = ("You will operate and secure hybrid environments for our customers, spanning private "
              "cloud and public cloud workloads with Terraform and Kubernetes. You will join a team "
              "of platform engineers delivering observability, secrets management and CI/CD "
              "pipelines for regulated clients.")
CLOUD_FUJITSU = ("Our client runs a multi-cloud and hybrid environments estate. You will design "
                 "automated provisioning with Ansible and Terraform, improve monitoring and support "
                 "the migration of legacy workloads. A background in infrastructure engineering and "
                 "scripting is what we ask for.")
HYBRID_OFFICE_DAYS = ("This is a hybrid role: three days a week in the office in Milan, two days "
                      "from home. You will build forecasting models with Python and PyTorch and work "
                      "with the data team on productionising them.")
HYBRID_TWO_DAYS = ("We work hybrid, 2 days on site each week in our Rome office and the rest from "
                   "home. You will analyse customer data with SQL and Python, build dashboards and "
                   "support the product team.")


@dataclass(frozen=True)
class Case:
    id: str
    title: str
    location: str
    tier: int
    hint: str | None
    text: str


AUDIT_CASES = [
    Case("40844", "AI Engineer", "Italy - Remote", 1, None, SIBILL),
    # The audit says "tier 1 unless noted" and does not note these two. They sit
    # in tier 3 here on purpose: that keeps their excerpts out of the rule
    # layer's confirm path (Kalepa would be confirmed in tier 1), so the test
    # exercises the model path and the post-model guard, which is what the
    # audit's rejections came from.
    Case("42883", "Junior Backend Engineer (Europe)", "Europe", 3, "remote", KALEPA),
    Case("42919", "Python and Kubernetes Software Engineer", "Worldwide", 3,
         "remote_worldwide", CANONICAL),
    Case("43155", "Forward Deployed Agile Software Engineer", "Italy", 1, "remote", JOBGETHER),
    Case("44288", "Data Platform Engineer", "Italy", 1, "remote", SHIPPYPRO),
    Case("45585", "Junior Digital Innovation Engineer", "Worldwide", 1,
         "remote_worldwide", AFFIDEA),
    Case("47100", "Junior AI Engineer - Pharma (Full remote)", "Madrid, Spain", 3, "hybrid", AMBIT),
    Case("43058", "MLOps Engineer", "Brussels, Belgium", 3, None, CLOUD_NOVA),
    Case("43139", "DevOps Engineer", "Brussels, Belgium", 3, None, CLOUD_FUJITSU),
]
GENUINE_HYBRIDS = [Case("control-office-days", "AI Engineer", "Milan, Italy", 1, None,
                        HYBRID_OFFICE_DAYS),
                   Case("control-two-days", "Data Scientist", "Rome, Italy", 1, None,
                        HYBRID_TWO_DAYS)]


def _rule(case: Case):
    return classify(case.text, title=case.title, location=case.location,
                    remote_hint=case.hint, require_italy_eligibility=case.tier in (3, 4),
                    # The source-hint shortcut has its own switch and its own
                    # test; keep the text-rule property independent of it, as
                    # production does (`src/processor/remote.py`).
                    hints_enabled=False)


def test_no_audit_case_is_rejected_by_the_rule_layer():
    for case in AUDIT_CASES:
        result = _rule(case)
        assert result is None or result.verdict != REJECTED, case.id


def test_the_cloud_hybrid_cases_are_silent_not_rejected():
    for case in [c for c in AUDIT_CASES if c.id in ("43058", "43139")]:
        assert _rule(case).verdict == NOT_STATED, case.id


def test_genuine_hybrids_still_reject():
    for case in GENUINE_HYBRIDS:
        assert _rule(case).verdict == REJECTED, case.id


# --- processor layer ------------------------------------------------------

@pytest.fixture(scope="module")
def cfg():
    return load_processor_config()


def _described(conn, cfg, n, description, **overrides):
    values = dict(source="linkedin", source_job_id=f"audit{n}",
                  source_url=f"https://www.linkedin.com/jobs/view/audit{n}",
                  title=f"AI Engineer {n}", company=f"Company {n}", location="Milan, Italy",
                  tier_hint=1, description=description, description_source="linkedin")
    values.update(overrides)
    job_id = record_sighting(conn, CardInput(**values), NOW).job_id
    check_one(conn, job_id, cfg, NOW)
    conn.commit()
    return job_id


def _row(conn, job_id):
    with conn.cursor() as cur:
        cur.execute("SELECT stage, stage_reason, remote_verdict, remote_reason, remote_advice "
                    "FROM jobs WHERE id = %s", (job_id,))
        return cur.fetchone()


def _rejecting_verify(reason):
    def verify(**kwargs):
        for offer in kwargs["offers"]:
            offer.remote_verdict = "rejected"
            offer.remote_reason = reason
        return kwargs["offers"], {"degraded": False}
    return verify


def test_the_hinted_silent_case_is_advised_without_a_model_call(store_conn, cfg):
    case = next(c for c in AUDIT_CASES if c.id == "45585")
    job = _described(store_conn, cfg, 1, case.text, title=case.title, location=case.location,
                     remote_hint=case.hint)
    assert apply_deterministic_rules(store_conn, cfg, NOW) == 1
    stage, _reason, verdict, stored, advice = _row(store_conn, job)
    assert (stage, verdict) == ("described", ADVISED)
    assert stored.startswith("Source marks the job remote") and advice


@pytest.mark.parametrize("case_id,reason", [
    ("42883", "Onsite team events in Warsaw and global team offsites once a year."),
    ("42919", "Teams meet two to four times yearly in person for team sprints."),
    # 43155 and 44288 are settled by the rule layer on these excerpts, so the
    # test runs them through the model path with the audit's own rejection
    # reasons (both were model rejections in production, 18 Oct).
    ("43155", "occasional travel for client workshops"),
    ("44288", "expenses-paid trips to HQ for team meetups"),
])
def test_the_source_hinted_cases_end_advised_not_rejected(store_conn, cfg, case_id, reason):
    case = next(c for c in AUDIT_CASES if c.id == case_id)
    job = _described(store_conn, cfg, 2, case.text, title=case.title, location=case.location,
                     tier_hint=case.tier, remote_hint=case.hint)
    later = NOW + timedelta(minutes=31)
    assert run_remote_batches(store_conn, cfg, later, LlmKeys("l", "g"),
                              verify=_rejecting_verify(reason)) == 1
    stage, _reason, verdict, stored, _advice = _row(store_conn, job)
    assert (stage, verdict) == ("described", ADVISED), case_id
    assert stored.startswith("Source marks the job remote")


def test_the_structured_hint_no_longer_overrides_an_explicit_full_remote(store_conn, cfg):
    case = next(c for c in AUDIT_CASES if c.id == "47100")
    job = _described(store_conn, cfg, 3, case.text, title=case.title, location=case.location,
                     tier_hint=case.tier, remote_hint=case.hint)
    assert apply_deterministic_rules(store_conn, cfg, NOW) == 0
    assert _row(store_conn, job)[2] is None


def test_a_genuine_hybrid_with_a_remote_hint_still_rejects(store_conn, cfg):
    job = _described(store_conn, cfg, 4, HYBRID_OFFICE_DAYS, remote_hint="remote")
    later = NOW + timedelta(minutes=31)
    assert run_remote_batches(store_conn, cfg, later, LlmKeys("l", "g"),
                              verify=_rejecting_verify("Occasional travel to the office.")) == 1
    assert _row(store_conn, job)[0] == "filtered_out"
    assert _row(store_conn, job)[2] == REJECTED
