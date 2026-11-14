"""The offline analysis of a failed run's jobs: excerpt, rules and signature (SPEC §7.1 6-7).

This is the part of the pipeline that needs no GitHub and no LLM. The diagnose use case (P1c)
and the eval runner both call it; the LLM step (policy, prompt, validation) builds on its result.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from cicd_doctor.core.logs import ExcerptConfig, build_excerpts
from cicd_doctor.core.models import (
    Diagnosis,
    FailedJob,
    LogExcerpt,
    RawLog,
    RuleMatch,
    Signature,
)
from cicd_doctor.core.ports import TokenCounter
from cicd_doctor.core.rules import RULE_PATTERNS, RULES, Rule, match_rules, rules_diagnosis
from cicd_doctor.core.signature import compute_signature


@dataclass(frozen=True)
class JobAnalysis:
    job: FailedJob
    excerpt: LogExcerpt
    matches: list[RuleMatch]  # best first
    rules: Diagnosis  # source "rules"; always computed (SPEC §7.1 step 7)
    signature: Signature


def analyze_jobs(
    logs: Sequence[tuple[FailedJob, RawLog]],
    counter: TokenCounter,
    config: ExcerptConfig | None = None,
    rules: Sequence[Rule] = RULES,
) -> list[JobAnalysis]:
    """Excerpt every job (one shared token budget), run the rules, and sign each failure.

    The rule patterns also flag error lines, so a line a rule can match is kept in the excerpt.
    The signature's error type is the best rule id when a rule matched (SPEC §7.4).
    """
    patterns = RULE_PATTERNS if rules is RULES else [p for r in rules for p in r.all_patterns()]
    excerpts = build_excerpts(logs, counter, config or ExcerptConfig(), patterns)
    analyses: list[JobAnalysis] = []
    for (job, _), excerpt in zip(logs, excerpts, strict=True):
        matches = match_rules(job, excerpt, rules)
        top_rule = matches[0].rule_id if matches else None
        analyses.append(
            JobAnalysis(
                job=job,
                excerpt=excerpt,
                matches=matches,
                rules=rules_diagnosis(excerpt, matches),
                signature=compute_signature(job, excerpt, top_rule),
            )
        )
    return analyses
