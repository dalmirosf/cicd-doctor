"""Rule engine (SPEC §10, D-60).

A rule is a set of regexes plus a category, a base confidence and a priority. The engine scans
the excerpt (cleaned and redacted lines), builds one ``RuleMatch`` per rule that fires, and
ranks them by (priority, confidence, earliest line). Rules never execute anything from the log
and never output ``flaky`` (D-15).
"""

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from cicd_doctor.core.models import (
    Category,
    Diagnosis,
    Evidence,
    FailedJob,
    LogExcerpt,
    RuleMatch,
)

MAX_SCAN_CHARS = 2000  # regexes only see the first characters of a line (no catastrophic input)
MAX_EVIDENCE = 8  # Diagnosis.evidence holds at most 8 items
MAX_EVIDENCE_CHARS = 300  # Evidence.text max length
MAX_GROUP_CHARS = 120  # a value captured from the log and put into a cause or fix
NEAR_END_LINES = 20  # "near the end of the step" for tail-only patterns

UNKNOWN_CAUSE = "No known failure pattern matched the log of the failed step."
UNKNOWN_FIX = (
    "Read the error lines of the failed step; the rule baseline has no match for this failure."
)


@dataclass(frozen=True)
class RuleContext:
    job: FailedJob
    excerpt: LogExcerpt

    def mentions(self, word: str) -> bool:
        """True when the step name or any excerpt line contains ``word`` (case-insensitive)."""
        needle = word.lower()
        if self.job.step_name and needle in self.job.step_name.lower():
            return True
        return any(needle in line.text[:MAX_SCAN_CHARS].lower() for line in self.excerpt.lines)


@dataclass(frozen=True)
class Rule:
    id: str
    ecosystem: str
    patterns: tuple[re.Pattern[str], ...]
    category: Category
    confidence: float
    priority: int  # 3 = decisive, 2 = specific cause, 1 = symptom or fallback (D-60)
    cause: str  # template; {name} is filled from the named groups of the first match
    fix: str
    # Patterns that only count in the last NEAR_END_LINES lines of the excerpt.
    tail_patterns: tuple[re.Pattern[str], ...] = ()
    # The rule also fires, without a matching line, when the job has this conclusion.
    on_conclusion: str | None = None
    # The rule applies only when this returns True.
    when: Callable[[RuleContext], bool] | None = None
    # Adjusts a match using the whole context (for example to change its category).
    refine: Callable[[RuleContext, RuleMatch], RuleMatch] | None = None
    defaults: Mapping[str, str] = field(default_factory=dict)  # for groups that did not match

    def all_patterns(self) -> tuple[re.Pattern[str], ...]:
        return self.patterns + self.tail_patterns


class _Template(dict[str, str]):
    """Fills a cause or fix template; a placeholder without a value becomes ``?``."""

    def __missing__(self, key: str) -> str:
        return "?"


def match_rule(rule: Rule, context: RuleContext) -> RuleMatch | None:
    """The match of one rule on one excerpt, or None."""
    if rule.when is not None and not rule.when(context):
        return None
    lines = context.excerpt.lines
    tail_start = len(lines) - NEAR_END_LINES
    hits: list[int] = []
    captured: dict[str, str] = {}  # the first value of each named group, over all the lines
    for index, line in enumerate(lines):
        text = line.text[:MAX_SCAN_CHARS]
        patterns = rule.all_patterns() if index >= tail_start else rule.patterns
        found = [m for p in patterns if (m := p.search(text)) is not None]
        if not found:
            continue
        hits.append(line.number)
        for regex_match in found:
            for name, value in regex_match.groupdict().items():
                if value is not None and name not in captured:
                    captured[name] = value.strip()[:MAX_GROUP_CHARS]
    by_conclusion = rule.on_conclusion is not None and context.job.conclusion == rule.on_conclusion
    if not hits and not by_conclusion:
        return None
    values = _Template(rule.defaults)
    values.update(captured)
    match = RuleMatch(
        rule_id=rule.id,
        category=rule.category,
        confidence=rule.confidence,
        cause=rule.cause.format_map(values),
        fix=rule.fix.format_map(values),
        evidence_lines=hits[:MAX_EVIDENCE],
    )
    return rule.refine(context, match) if rule.refine is not None else match


def match_rules(job: FailedJob, excerpt: LogExcerpt, rules: Sequence[Rule]) -> list[RuleMatch]:
    """Every match, best first: highest priority, then confidence, then earliest evidence."""
    context = RuleContext(job=job, excerpt=excerpt)
    priority = {rule.id: rule.priority for rule in rules}
    matches = [m for rule in rules if (m := match_rule(rule, context)) is not None]

    def rank(match: RuleMatch) -> tuple[int, float, float]:
        earliest = min(match.evidence_lines, default=math.inf)
        return (-priority[match.rule_id], -match.confidence, earliest)

    return sorted(matches, key=rank)


def rules_diagnosis(excerpt: LogExcerpt, matches: Sequence[RuleMatch]) -> Diagnosis:
    """The rules diagnosis: the best match, or ``unknown`` with confidence 0 (SPEC §10).

    ``rule_ids`` lists every match, best first; the evidence is the best match's lines.
    """
    if not matches:
        return Diagnosis(
            category=Category.UNKNOWN,
            probable_cause=UNKNOWN_CAUSE,
            evidence=[],
            suggested_fix=UNKNOWN_FIX,
            confidence=0.0,
            source="rules",
        )
    best = matches[0]
    texts = {line.number: line.text for line in excerpt.lines}
    evidence = [
        Evidence(line=number, text=texts[number][:MAX_EVIDENCE_CHARS])
        for number in best.evidence_lines[:MAX_EVIDENCE]
        if number in texts
    ]
    return Diagnosis(
        category=best.category,
        probable_cause=best.cause[:500],
        evidence=evidence,
        suggested_fix=best.fix[:1000],
        confidence=best.confidence,
        source="rules",
        rule_ids=[match.rule_id for match in matches],
    )


def compile_all(*patterns: str, flags: int = 0) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(pattern, flags) for pattern in patterns)
