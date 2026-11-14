"""Eval variants (SPEC §11.2). P1b has ``rules``; the LLM variants arrive with the LLM client."""

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from cicd_doctor.core.analysis import analyze_jobs
from cicd_doctor.core.logs import select_failed_jobs
from cicd_doctor.core.models import FailedJob
from cicd_doctor.core.ports import TokenCounter
from evals.runner.cases import Case

ALL_VARIANTS = ("rules", "llm", "llm+hints", "llm+hints+similar")
NOT_YET = {
    "llm": "arrives with the LLM client (P1b, third PR)",
    "llm+hints": "arrives with the LLM client (P1b, third PR)",
    "llm+hints+similar": "arrives with similarity search (P2)",
}


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    source: str
    variant: str
    expected: str  # category
    predicted: str
    confidence: float
    rule_ids: list[str] = field(default_factory=list)
    evidence_lines: list[int] = field(default_factory=list)  # published evidence
    excerpt_lines: list[int] = field(default_factory=list)
    expected_evidence: list[int] = field(default_factory=list)
    signature_hash: str = ""
    token_count: int = 0
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    error: str | None = None

    @property
    def correct(self) -> bool:
        return self.error is None and self.predicted == self.expected

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


Variant = Callable[[Case, TokenCounter], CaseResult]


def pick_job(case: Case) -> FailedJob | None:
    """The job of ``log.txt``: ``job_id`` from case.yaml, else the first failed job."""
    failed = select_failed_jobs(case.run_ref(), case.jobs, limit=len(case.jobs) or 1)
    if case.meta.job_id is not None:
        return next((job for job in failed if job.job.job_id == case.meta.job_id), None)
    return failed[0] if failed else None


def run_rules(case: Case, counter: TokenCounter) -> CaseResult:
    """The rule baseline: excerpt, rules and signature, no LLM (SPEC §10)."""
    base = {
        "case_id": case.id,
        "source": case.meta.source,
        "variant": "rules",
        "expected": case.meta.expected.category.value,
        "expected_evidence": list(case.meta.expected.evidence_lines),
    }
    job = pick_job(case)
    if job is None:
        return CaseResult(
            **base,  # type: ignore[arg-type]
            predicted="error",
            confidence=0.0,
            error="no failed job matches the case",
        )
    started = time.perf_counter()
    [analysis] = analyze_jobs([(job, case.log)], counter)
    latency_ms = (time.perf_counter() - started) * 1000
    return CaseResult(
        **base,  # type: ignore[arg-type]
        predicted=analysis.rules.category.value,
        confidence=analysis.rules.confidence,
        rule_ids=list(analysis.rules.rule_ids),
        evidence_lines=[item.line for item in analysis.rules.evidence],
        excerpt_lines=[line.number for line in analysis.excerpt.lines],
        signature_hash=analysis.signature.hash,
        token_count=analysis.excerpt.token_count,
        latency_ms=round(latency_ms, 2),
    )


VARIANTS: dict[str, Variant] = {"rules": run_rules}
