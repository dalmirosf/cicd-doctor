"""Eval variants (SPEC §11.2): ``rules``, ``llm`` (no hints) and ``llm+hints`` (D-14).

The LLM variants run the production pipeline (``DiagnoseJob``) so the numbers describe what the app
would publish: policy, budget, validation and fallbacks included. ``llm+hints+similar`` also gives
each case the exact-signature cache and the similar past failures of an in-memory history
(``evals/runner/history.py``, D-86). A case that fell back to the rules diagnosis shows its
``fallback_reason``, and an injection case is judged (``wins``).
"""

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from uuid import uuid4

from cicd_doctor.core.analysis import JobAnalysis, analyze_jobs
from cicd_doctor.core.diagnosis.merge import baseline_to_show
from cicd_doctor.core.diagnosis.pipeline import DiagnoseJob, DiagnosisContext
from cicd_doctor.core.diagnosis.validate import (
    InvalidOutputError,
    parse_output,
    verified_evidence,
)
from cicd_doctor.core.diff import select_for_prompt
from cicd_doctor.core.logs import select_failed_jobs
from cicd_doctor.core.models import (
    DiagnosisReport,
    FailedJob,
    Failure,
    FailureStatus,
    LogExcerpt,
)
from cicd_doctor.core.ports import TokenCounter
from cicd_doctor.core.render import render_check, render_comment
from evals.runner.cases import Case
from evals.runner.history import Lookup, SimilarHistory
from evals.runner.recording import RecordingLLM

ALL_VARIANTS = ("rules", "llm", "llm+hints", "llm+hints+similar")
LLM_VARIANTS = ("llm", "llm+hints", "llm+hints+similar")
SIMILAR_VARIANT = "llm+hints+similar"


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
    similar_found: int = 0  # past failures the history offered (``llm+hints+similar`` only, D-86)
    similar_top_score: float | None = None  # the best of them
    fallback_reason: str | None = None
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    attack: bool = False  # an injection case
    wins: list[str] = field(default_factory=list)  # how the attack succeeded (empty = it failed)
    # What the answer says, for the cause judge (D-103). Model text: never written to a report.
    answer_cause: str = ""
    answer_fix: str = ""
    # P4 (b), D-109. ``llm_latency_ms`` None means no answer of the model was recorded (rules,
    # cache hit, or a call that failed). The items are the evidence of that answer before and
    # after ``verified_evidence``.
    answer_raw_items: int = 0
    answer_valid_items: int = 0
    llm_latency_ms: int | None = None
    occurred_at: str | None = None  # when the failure happened (public cases, D-101); optional

    @property
    def correct(self) -> bool:
        return self.error is None and self.predicted == self.expected

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        del data["answer_cause"], data["answer_fix"]  # untrusted text stays out of the reports
        return data


Variant = Callable[[Case, TokenCounter], CaseResult]


def pick_job(case: Case) -> FailedJob | None:
    """The job of ``log.txt``: ``job_id`` from case.yaml, else the first failed job."""
    failed = select_failed_jobs(
        case.run_ref(), case.jobs, limit=len(case.jobs) or 1, annotations=case.annotations
    )
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
        answer_cause=analysis.rules.probable_cause,
        answer_fix=analysis.rules.suggested_fix,
    )


async def run_llm_case(
    case: Case,
    counter: TokenCounter,
    *,
    variant: str,
    pipeline: DiagnoseJob,
    use_hints: bool,
    history: SimilarHistory | None = None,
    recorder: RecordingLLM | None = None,
) -> CaseResult:
    """One case through the production pipeline, as a non-fork run on an installation.

    With a ``history`` the case gets the cache and the similar failures of the cases before it, and
    leaves its own diagnosis for the cases after it (D-86).
    """
    analysis = _analyse(case, counter)
    if analysis is None:
        return _no_job(case, variant)
    lookup = Lookup(None, [], None)
    if history is not None:
        lookup = await history.lookup(case, analysis)
    # The diff of an injection case is the pull request of the run: the model sees the hunks of
    # the files the log names and a suggestion is judged against them (D-97, D-99).
    shown = select_for_prompt(case.diff, analysis.excerpt, counter)
    started = time.perf_counter()
    result = await pipeline.run(
        analysis,
        DiagnosisContext(
            installation_id=0,
            is_fork=False,
            cached=lookup.cached,
            similar=lookup.similar,
            diff=shown,
        ),
        use_hints=use_hints,
    )
    if history is not None:
        await history.remember(case, analysis, result, lookup)
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
        wins = judge(case, published, markdown, analysis.excerpt, shown)
    answer = raw_answer(recorder, case.id, analysis.excerpt)
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
        similar_found=len(lookup.similar),
        similar_top_score=lookup.similar[0].score if lookup.similar else None,
        fallback_reason=published.fallback_reason,
        llm_calls=usage.calls if usage else 0,
        input_tokens=usage.input_tokens if usage else 0,
        output_tokens=usage.output_tokens if usage else 0,
        wins=wins,
        answer_cause=published.probable_cause,
        answer_fix=published.suggested_fix,
        answer_raw_items=answer.raw_items,
        answer_valid_items=answer.valid_items,
        llm_latency_ms=answer.latency_ms,
    )


@dataclass(frozen=True)
class RawAnswer:
    raw_items: int = 0
    valid_items: int = 0
    latency_ms: int | None = None


def raw_answer(recorder: RecordingLLM | None, case_id: str, excerpt: LogExcerpt) -> RawAnswer:
    """What the model said about the evidence, before the pipeline dropped what it could not verify.

    The accepted answer is the last one the recorder saw for the case. An answer that does not
    parse has a latency but no items: the pipeline repaired it or fell back, and
    ``verified_evidence`` never saw it.
    """
    recording = recorder.cases.get(case_id) if recorder is not None else None
    accepted = recording.accepted if recording is not None else None
    if accepted is None:
        return RawAnswer()
    try:
        output = parse_output(accepted.raw_json)
    except InvalidOutputError:
        return RawAnswer(latency_ms=accepted.latency_ms)
    return RawAnswer(
        raw_items=len(output.evidence),
        valid_items=len(verified_evidence(output.evidence, excerpt)),
        latency_ms=accepted.latency_ms,
    )


VARIANTS: dict[str, Variant] = {"rules": run_rules}
