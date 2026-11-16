"""Eval variants (SPEC §11.2): ``rules``, ``llm`` (no hints) and ``llm+hints`` (D-14).

``llm+hints+similar`` arrives with similarity search (P2). The LLM variants run the production
pipeline (``DiagnoseJob``) so the numbers describe what the app would publish: policy, budget,
validation and fallbacks included. A case that fell back to the rules diagnosis shows its
``fallback_reason``, and an injection case is judged (``wins``).
"""

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from uuid import uuid4

from cicd_doctor.core.analysis import JobAnalysis, analyze_jobs
from cicd_doctor.core.diagnosis.merge import baseline_to_show
from cicd_doctor.core.diagnosis.pipeline import DiagnoseJob, DiagnosisContext
from cicd_doctor.core.logs import select_failed_jobs
from cicd_doctor.core.models import (
    DiagnosisReport,
    FailedJob,
    Failure,
    FailureStatus,
)
from cicd_doctor.core.ports import TokenCounter
from cicd_doctor.core.render import render_check, render_comment
from evals.runner.cases import Case

ALL_VARIANTS = ("rules", "llm", "llm+hints", "llm+hints+similar")
LLM_VARIANTS = ("llm", "llm+hints")
NOT_YET = {"llm+hints+similar": "arrives with similarity search (P2)"}


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    source: str  # where the case comes from: lab, public or injection
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
    diagnosis_source: str = "rules"  # rules, llm or cache: what was published
    fallback_reason: str | None = None
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    attack: bool = False  # an injection case
    wins: list[str] = field(default_factory=list)  # how the attack succeeded (empty = it failed)

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


def _base(case: Case, variant: str) -> dict[str, object]:
    return {
        "case_id": case.id,
        "source": case.meta.source,
        "variant": variant,
        "expected": case.meta.expected.category.value,
        "expected_evidence": list(case.meta.expected.evidence_lines),
        "attack": case.meta.attack is not None,
    }


def _no_job(case: Case, variant: str) -> CaseResult:
    return CaseResult(
        **_base(case, variant),  # type: ignore[arg-type]
        predicted="error",
        confidence=0.0,
        error="no failed job matches the case",
    )


def _analyse(case: Case, counter: TokenCounter) -> JobAnalysis | None:
    job = pick_job(case)
    if job is None:
        return None
    [analysis] = analyze_jobs([(job, case.log)], counter)
    return analysis


def run_rules(case: Case, counter: TokenCounter) -> CaseResult:
    """The rule baseline: excerpt, rules and signature, no LLM (SPEC §10)."""
    started = time.perf_counter()
    analysis = _analyse(case, counter)
    if analysis is None:
        return _no_job(case, "rules")
    latency_ms = (time.perf_counter() - started) * 1000
    return CaseResult(
        **_base(case, "rules"),  # type: ignore[arg-type]
        predicted=analysis.rules.category.value,
        confidence=analysis.rules.confidence,
        rule_ids=list(analysis.rules.rule_ids),
        evidence_lines=[item.line for item in analysis.rules.evidence],
        excerpt_lines=[line.number for line in analysis.excerpt.lines],
        signature_hash=analysis.signature.hash,
        token_count=analysis.excerpt.token_count,
        latency_ms=round(latency_ms, 2),
    )


async def run_llm_case(
    case: Case,
    counter: TokenCounter,
    *,
    variant: str,
    pipeline: DiagnoseJob,
    use_hints: bool,
) -> CaseResult:
    """One case through the production pipeline, as a non-fork run on an installation."""
    analysis = _analyse(case, counter)
    if analysis is None:
        return _no_job(case, variant)
    started = time.perf_counter()
    result = await pipeline.run(
        analysis, DiagnosisContext(installation_id=0, is_fork=False), use_hints=use_hints
    )
    latency_ms = (time.perf_counter() - started) * 1000
    published, usage = result.published, result.usage
    wins: list[str] = []
    if case.meta.attack is not None:
        from evals.runner.injection import judge  # the judge needs the renderer

        failure = Failure(
            id=uuid4(),
            job=analysis.job,
            excerpt=analysis.excerpt,
            signature=analysis.signature,
            embedding=None,
            status=FailureStatus.OPEN,
            fix_commit_sha=None,
        )
        baseline = baseline_to_show(result.rules, published)
        report = DiagnosisReport(
            run=analysis.job.job.run,
            items=[(failure, published)],
            rules_baseline={failure.id: baseline} if baseline else {},
        )
        markdown = render_check(report).summary + render_comment(report)
        wins = judge(case, published, markdown, analysis.excerpt)
    return CaseResult(
        **_base(case, variant),  # type: ignore[arg-type]
        predicted=published.category.value,
        confidence=published.confidence,
        rule_ids=list(published.rule_ids),
        evidence_lines=[item.line for item in published.evidence],
        excerpt_lines=[line.number for line in analysis.excerpt.lines],
        signature_hash=analysis.signature.hash,
        token_count=analysis.excerpt.token_count,
        latency_ms=round(latency_ms, 2),
        cost_usd=float(usage.cost_usd) if usage else 0.0,
        diagnosis_source=published.source,
        fallback_reason=published.fallback_reason,
        llm_calls=usage.calls if usage else 0,
        input_tokens=usage.input_tokens if usage else 0,
        output_tokens=usage.output_tokens if usage else 0,
        wins=wins,
    )


VARIANTS: dict[str, Variant] = {"rules": run_rules}
