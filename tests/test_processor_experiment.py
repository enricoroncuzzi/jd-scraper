import json
from datetime import datetime, timezone

import pytest

from src.models import ScoredOffer
from src.processor.config import load_processor_config
from src.processor.experiment import (
    RequestCounter, SampleJob, ScoreReport, VerdictReport, decide, load_sample, read_sample,
    run_merged_experiment, run_score_experiment, run_verdict_experiment, write_sample,
)
from src.processor.llm_stages import LlmKeys
from tests.legacy_schema import add_offer, add_run, create_legacy_tables
from tests.test_processor_combined import FakeChain, _output

KEYS = LlmKeys("k", "")


@pytest.fixture(scope="module")
def cfg():
    return load_processor_config()


def _job(n, tier=1, verdict="confirmed", score=8):
    return SampleJob(f"https://x/{n}", f"Role {n}", "Acme", "Milan, Italy", "Fully remote role.", tier,
                     verdict, score)


class CountingVerify:
    """verify_offers stand-in: counts one request per batch via the counter."""

    def __init__(self, counter, verdict_for=lambda offer: "confirmed"):
        self.counter, self.verdict_for, self.batches = counter, verdict_for, []

    def __call__(self, offers, require_italy_eligibility, groq_api_key, llm_api_key, batch_size=None):
        self.batches.append((len(offers), require_italy_eligibility, batch_size))
        self.counter(None)
        for offer in offers:
            offer.remote_verdict = self.verdict_for(offer)
        return offers, {}


def test_verdict_agreement_is_counted_per_job_and_batches_follow_the_size(cfg):
    counter = RequestCounter()
    sample = [_job(n) for n in range(12)] + [_job(n, tier=3) for n in range(12, 16)]
    verify = CountingVerify(counter, lambda o: "rejected" if o.title == "Role 3" else "confirmed")
    report = run_verdict_experiment(sample, cfg, KEYS, batch_size=8, counter=counter, max_requests=50,
                                    verify=verify)
    assert (report.compared, report.agreed, report.requests) == (16, 15, 3)
    assert verify.batches == [(8, False, 8), (4, False, 8), (4, True, 8)]
    assert report.disagreements == [{"key": "https://x/3", "stored": "confirmed", "new": "rejected"}]
    assert report.rate == pytest.approx(15 / 16) and not report.passes


def test_agreement_must_be_strictly_above_95_percent(cfg):
    ok = VerdictReport(8, compared=100, agreed=96)
    edge = VerdictReport(8, compared=100, agreed=95)
    assert ok.passes and not edge.passes


def test_tier_two_jobs_are_not_part_of_the_remote_experiment(cfg):
    counter = RequestCounter()
    verify = CountingVerify(counter)
    report = run_verdict_experiment([_job(1, tier=2, verdict="not_checked")], cfg, KEYS, batch_size=8,
                                    counter=counter, max_requests=10, verify=verify)
    assert report.compared == 0 and verify.batches == []


def test_the_request_cap_stops_the_run_and_marks_it_inconclusive(cfg):
    counter = RequestCounter()
    verify = CountingVerify(counter)
    report = run_verdict_experiment([_job(n) for n in range(40)], cfg, KEYS, batch_size=8,
                                    counter=counter, max_requests=2, verify=verify)
    assert report.stopped_early and report.requests == 2 and not report.passes


class CountingScore:
    def __init__(self, counter, delta=0):
        self.counter, self.delta, self.batch_sizes = counter, delta, []

    def __call__(self, offers, profile, priority_keywords, exclude_keywords, llm_api_key, batch_size=None):
        self.batch_sizes.append(batch_size)
        self.counter(None)
        return [ScoredOffer(**o.model_dump(), score=min(10, 8 + self.delta)) for o in offers], {}


def test_scores_within_one_point_count_as_agreement(cfg):
    counter = RequestCounter()
    sample = [_job(n, score=8) for n in range(10)]
    ok = run_score_experiment(sample, cfg, KEYS, batch_size=8, counter=counter, max_requests=20,
                              score=CountingScore(counter, delta=1))
    far = run_score_experiment(sample, cfg, KEYS, batch_size=8, counter=counter, max_requests=20,
                               score=CountingScore(counter, delta=2))
    assert ok.rate == 1.0 and ok.passes
    assert far.rate == 0.0 and not far.passes


def test_the_merged_call_is_compared_on_both_measures_at_once(cfg):
    counter = RequestCounter()

    class CountingChain(FakeChain):
        def invoke(self, payload, config=None):
            counter(None)
            return super().invoke(payload, config)

    chain = CountingChain(_output(5, "confirmed", 8), _output(2, "confirmed", 8))
    sample = [_job(n) for n in range(7)]
    verdicts, scores = run_merged_experiment(sample, cfg, KEYS, batch_size=5, counter=counter,
                                             max_requests=10, chain=chain)
    assert (verdicts.compared, verdicts.rate, scores.rate) == (7, 1.0, 1.0)
    assert verdicts.requests == 2


def test_decide_adopts_only_when_everything_passes():
    good_v, good_s = VerdictReport(12, compared=100, agreed=97), ScoreReport(8, compared=100, within_one=92)
    bad_s = ScoreReport(8, compared=100, within_one=85)
    assert decide(good_v, good_s)["adopt"] is True
    assert decide(good_v, bad_s)["adopt"] is False
    assert decide(None, None)["adopt"] is False


def test_the_sample_is_reproducible_and_round_trips(tmp_path, store_conn):
    create_legacy_tables(store_conn)
    when = datetime.now(timezone.utc)
    run = add_run(store_conn, 1, when)
    for n in range(30):
        add_offer(store_conn, run, f"https://www.linkedin.com/jobs/view/x-39{n:08d}", when=when, score=7)
    add_offer(store_conn, run, "https://www.linkedin.com/jobs/view/x-3999999999", when=when, tier=2)
    store_conn.commit()
    first = load_sample(store_conn, "legacy", days=14, size=10, seed=1)
    second = load_sample(store_conn, "legacy", days=14, size=10, seed=1)
    assert first == second and len(first) == 10 and all(job.tier in (1, 3, 4) for job in first)
    path = tmp_path / "sample.jsonl"
    write_sample(str(path), first)
    assert read_sample(str(path)) == first
    assert json.loads(path.read_text().splitlines()[0])["stored_verdict"] == "confirmed"


def test_the_cli_runs_a_remote_experiment_and_prints_the_decision(tmp_path, capsys, cfg):
    from src.processor.experiment import write_sample
    from src.processor.experiment_cli import main

    sample = tmp_path / "s.jsonl"
    write_sample(str(sample), [_job(n) for n in range(10)])

    def verify(offers, require_italy_eligibility, groq_api_key, llm_api_key, batch_size=None):
        from src import telemetry
        telemetry._llm_sink(None)
        for offer in offers:
            offer.remote_verdict = "confirmed"
        return offers, {}

    code = main(["remote", "--sample", str(sample), "--batch-size", "8", "--max-requests", "10"],
                keys=KEYS, verify=verify)
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["experiment"] == "remote"
    assert out["verdict"]["compared"] == 10 and out["decision"]["adopt"] is True
