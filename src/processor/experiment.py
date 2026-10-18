"""Gated experiments for spec sections 6.3 (bigger batches) and 6.4 (one merged
call). Nothing here changes production behavior: it replays a fixed sample of past
jobs through a candidate configuration and compares against the stored verdicts
and scores. A candidate is adopted only when BOTH thresholds hold.

A baseline run of the CURRENT configuration on the same sample shows the noise
floor (the free models are not deterministic), so a miss can be read properly.
"""
import json
import random
from dataclasses import asdict, dataclass, field
from types import SimpleNamespace

from src import telemetry
from src.processor.combined import invoke_combined
from src.processor.config import ProcessorConfig
from src.processor.llm_stages import LlmKeys, to_offers
from src.remote_verifier import verify_offers
from src.scorer import score_offers

VERDICT_AGREEMENT_MIN = 0.95
SCORE_WITHIN_ONE_MIN = 0.90

LEGACY_SAMPLE_SQL = """
SELECT link, title, company, location, description, tier, remote_verdict, score
  FROM offers
 WHERE description_status = 'ok' AND description <> '' AND score IS NOT NULL
   AND tier IN (1, 3, 4) AND remote_verdict IN ('confirmed', 'unconfirmed')
   AND run_at >= now() - make_interval(days => %s)
"""
STORE_SAMPLE_SQL = """
SELECT canonical_url, title, company, location_raw, description, tier, remote_verdict, score
  FROM jobs
 WHERE description_status = 'ok' AND description <> '' AND tier IN (1, 3, 4)
   AND remote_verdict IN ('confirmed', 'advised', 'not_stated', 'rejected', 'unconfirmed')
   AND first_seen_at >= now() - make_interval(days => %s)
"""


@dataclass(frozen=True)
class SampleJob:
    key: str
    title: str
    company: str
    location: str
    description: str
    tier: int
    stored_verdict: str | None
    stored_score: int | None


class RequestCounter:
    """Installed as the llm sink so every provider request is counted."""

    def __init__(self):
        self.requests = 0

    def __call__(self, _data: dict) -> None:
        self.requests += 1


@dataclass
class VerdictReport:
    batch_size: int
    compared: int = 0
    agreed: int = 0
    requests: int = 0
    stopped_early: bool = False
    disagreements: list[dict] = field(default_factory=list)

    @property
    def rate(self) -> float:
        return self.agreed / self.compared if self.compared else 0.0

    @property
    def passes(self) -> bool:
        return not self.stopped_early and self.compared > 0 and self.rate > VERDICT_AGREEMENT_MIN


@dataclass
class ScoreReport:
    batch_size: int
    compared: int = 0
    within_one: int = 0
    requests: int = 0
    stopped_early: bool = False

    @property
    def rate(self) -> float:
        return self.within_one / self.compared if self.compared else 0.0

    @property
    def passes(self) -> bool:
        return not self.stopped_early and self.compared > 0 and self.rate >= SCORE_WITHIN_ONE_MIN


def load_sample(conn, source: str, days: int, size: int, seed: int) -> list[SampleJob]:
    """A reproducible random sample. source is 'legacy' (Neon `offers`, which holds
    no rejected verdicts) or 'store' (the job store, which does)."""
    sql = LEGACY_SAMPLE_SQL if source == "legacy" else STORE_SAMPLE_SQL
    with conn.cursor() as cur:
        cur.execute(sql, (days,))
        rows = sorted(cur.fetchall(), key=lambda row: row[0])
    chosen = random.Random(seed).sample(rows, min(size, len(rows)))
    return [SampleJob(link, title or "", company or "", location or "N/A", description,
                      tier, verdict, score)
            for link, title, company, location, description, tier, verdict, score in chosen]


def write_sample(path: str, sample: list[SampleJob]) -> None:
    with open(path, "w") as handle:
        for job in sample:
            handle.write(json.dumps(asdict(job)) + "\n")


def read_sample(path: str) -> list[SampleJob]:
    with open(path) as handle:
        return [SampleJob(**json.loads(line)) for line in handle if line.strip()]


def _rows_like(jobs: list[SampleJob]):
    """SampleJobs shaped like the JobRow fields to_offers reads."""
    return [SimpleNamespace(title=j.title, company=j.company, location_raw=j.location,
                            canonical_url=j.key, description=j.description, work_mode="")
            for j in jobs]


def _chunks(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def run_verdict_experiment(sample: list[SampleJob], cfg: ProcessorConfig, keys: LlmKeys, *,
                           batch_size: int, counter: RequestCounter, max_requests: int,
                           verify=verify_offers) -> VerdictReport:
    report = VerdictReport(batch_size)
    start = counter.requests
    for require_italy in (False, True):
        group = [j for j in sample if j.stored_verdict is not None
                 and cfg.policies[j.tier].remote_check_enabled
                 and cfg.policies[j.tier].require_italy_eligibility == require_italy]
        for chunk in _chunks(group, batch_size):
            if counter.requests - start >= max_requests:
                report.stopped_early = True
                report.requests = counter.requests - start
                return report
            offers, _usage = verify(
                offers=to_offers(_rows_like(chunk)), require_italy_eligibility=require_italy,
                groq_api_key="", llm_api_key=keys.llm_api_key, batch_size=batch_size)
            for job, offer in zip(chunk, offers):
                report.compared += 1
                if offer.remote_verdict == job.stored_verdict:
                    report.agreed += 1
                else:
                    report.disagreements.append(
                        {"key": job.key, "stored": job.stored_verdict, "new": offer.remote_verdict})
    report.requests = counter.requests - start
    return report


def run_score_experiment(sample: list[SampleJob], cfg: ProcessorConfig, keys: LlmKeys, *,
                         batch_size: int, counter: RequestCounter, max_requests: int,
                         score=score_offers) -> ScoreReport:
    report = ScoreReport(batch_size)
    start = counter.requests
    groups: dict[tuple, list[SampleJob]] = {}
    for job in sample:
        if job.stored_score is not None:
            groups.setdefault(cfg.policies[job.tier].scoring_key, []).append(job)
    for scoring_key, group in groups.items():
        policy = next(p for p in cfg.policies.values() if p.scoring_key == scoring_key)
        for chunk in _chunks(group, batch_size):
            if counter.requests - start >= max_requests:
                report.stopped_early = True
                report.requests = counter.requests - start
                return report
            scored, _usage = score(
                offers=to_offers(_rows_like(chunk)), profile=policy.candidate_profile,
                priority_keywords=list(policy.priority_keywords),
                exclude_keywords=list(policy.exclude_keywords), llm_api_key=keys.llm_api_key,
                batch_size=batch_size)
            by_index = {offer.id: offer for offer in scored}
            for index, job in enumerate(chunk):
                report.compared += 1
                if index in by_index and abs(by_index[index].score - job.stored_score) <= 1:
                    report.within_one += 1
    report.requests = counter.requests - start
    return report


def run_merged_experiment(sample: list[SampleJob], cfg: ProcessorConfig, keys: LlmKeys, *,
                          batch_size: int, counter: RequestCounter, max_requests: int,
                          chain) -> tuple[VerdictReport, ScoreReport]:
    verdicts, scores = VerdictReport(batch_size), ScoreReport(batch_size)
    start = counter.requests
    groups: dict[tuple, list[SampleJob]] = {}
    for job in sample:
        policy = cfg.policies[job.tier]
        if policy.remote_check_enabled and job.stored_verdict is not None:
            groups.setdefault((policy.require_italy_eligibility, policy.scoring_key), []).append(job)
    for (require_italy, scoring_key), group in groups.items():
        policy = next(p for p in cfg.policies.values() if p.scoring_key == scoring_key)
        for chunk in _chunks(group, batch_size):
            if counter.requests - start >= max_requests:
                verdicts.stopped_early = scores.stopped_early = True
                break
            items = invoke_combined(
                chain, to_offers(_rows_like(chunk)), require_italy_eligibility=require_italy,
                profile=policy.candidate_profile, priority_keywords=list(policy.priority_keywords),
                exclude_keywords=list(policy.exclude_keywords))
            by_index = {item.id: item for item in items}
            for index, job in enumerate(chunk):
                item = by_index.get(index)
                verdicts.compared += 1
                if item is not None and item.verdict == job.stored_verdict:
                    verdicts.agreed += 1
                elif item is not None:
                    verdicts.disagreements.append(
                        {"key": job.key, "stored": job.stored_verdict, "new": item.verdict})
                if job.stored_score is not None:
                    scores.compared += 1
                    if item is not None and abs(item.score - job.stored_score) <= 1:
                        scores.within_one += 1
    verdicts.requests = scores.requests = counter.requests - start
    return verdicts, scores


def decide(verdict: VerdictReport | None, score: ScoreReport | None) -> dict:
    """Adopt only when every measured threshold holds and no run stopped early."""
    checks = {}
    if verdict is not None:
        checks["verdict_agreement"] = {"rate": round(verdict.rate, 4), "needs": f"> {VERDICT_AGREEMENT_MIN}",
                                       "passes": verdict.passes, "compared": verdict.compared}
    if score is not None:
        checks["score_within_one"] = {"rate": round(score.rate, 4), "needs": f">= {SCORE_WITHIN_ONE_MIN}",
                                      "passes": score.passes, "compared": score.compared}
    return {"adopt": bool(checks) and all(c["passes"] for c in checks.values()), "checks": checks}


def install_counter() -> RequestCounter:
    counter = RequestCounter()
    telemetry.set_llm_sink(counter)
    return counter
