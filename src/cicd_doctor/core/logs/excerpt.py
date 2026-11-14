"""Build the redacted excerpt of each failed job (SPEC §7.3, D-11, D-57, D-58).

Per job: cap the log to its tail, slice the failed step, clean it, drop noise, find error lines,
keep the last ``tail_lines`` lines plus ``±error_context`` around each error, redact, and count
tokens. Then the token budget is shared between the jobs and each excerpt is trimmed: the oldest
context goes first, and error lines with their ``±protected_context`` neighbours go last.

Redaction happens inside this module, so a ``LogExcerpt`` is always redacted (D-12).
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from cicd_doctor.core.logs.clean import CleanLine, clean_lines
from cicd_doctor.core.logs.parse import parse_log, slice_step
from cicd_doctor.core.logs.tail import DEFAULT_MAX_BYTES, tail_text
from cicd_doctor.core.models import FailedJob, LogExcerpt, LogLine, RawLog
from cicd_doctor.core.ports import TokenCounter
from cicd_doctor.core.redaction import redact_lines

TRUNCATED_LINE_SUFFIX = " …[line truncated]"


@dataclass(frozen=True, slots=True)
class ExcerptConfig:
    max_tokens: int = 6000  # EXCERPT_MAX_TOKENS, shared by all the jobs of a run
    max_bytes: int = DEFAULT_MAX_BYTES  # LOG_MAX_BYTES, per job
    tail_lines: int = 150  # last lines of the step always considered
    error_context: int = 15  # lines kept around each error line
    protected_context: int = 3  # lines around each error that are cut last
    max_line_chars: int = 2000  # longer lines are cut (after redaction)

    def __post_init__(self) -> None:
        if self.max_tokens < 1 or self.max_bytes < 1 or self.max_line_chars < 1:
            raise ValueError("max_tokens, max_bytes and max_line_chars must be >= 1")
        if min(self.tail_lines, self.error_context, self.protected_context) < 0:
            raise ValueError("tail_lines, error_context and protected_context must be >= 0")


DEFAULT_CONFIG = ExcerptConfig()


def render_line(line: LogLine) -> str:
    """How an excerpt line is shown to the LLM and counted: ``L<number>: <text>``."""
    return f"L{line.number}: {line.text}"


@dataclass(frozen=True, slots=True)
class _Candidate:
    line: LogLine
    is_error: bool
    tokens: int
    redactions: int


@dataclass(frozen=True, slots=True)
class _Draft:
    job: FailedJob
    truncated: bool
    keep_order: list[_Candidate]  # most important first; the excerpt keeps a prefix of it

    @property
    def tokens(self) -> int:
        return sum(candidate.tokens for candidate in self.keep_order)


def build_excerpts(
    logs: Sequence[tuple[FailedJob, RawLog]],
    counter: TokenCounter,
    config: ExcerptConfig = DEFAULT_CONFIG,
    error_patterns: Sequence[re.Pattern[str]] = (),
) -> list[LogExcerpt]:
    """One excerpt per job, in the order given, with ``max_tokens`` in total across them."""
    drafts = [_draft(job, raw, counter, config, error_patterns) for job, raw in logs]
    budgets = share_budget([draft.tokens for draft in drafts], config.max_tokens)
    return [_finish(draft, budget) for draft, budget in zip(drafts, budgets, strict=True)]


def build_excerpt(
    job: FailedJob,
    raw: RawLog,
    counter: TokenCounter,
    config: ExcerptConfig = DEFAULT_CONFIG,
    error_patterns: Sequence[re.Pattern[str]] = (),
) -> LogExcerpt:
    """The excerpt of a single job, with the whole ``max_tokens`` budget."""
    return build_excerpts([(job, raw)], counter, config, error_patterns)[0]


def share_budget(needs: Sequence[int], total: int) -> list[int]:
    """Split ``total`` tokens between jobs that need ``needs`` tokens each.

    When everything fits, each job gets what it needs. Otherwise the budget is split evenly, and
    what a job does not use is shared again between the others (water-filling), so no budget is
    wasted on a short excerpt while another one is cut (D-57).
    """
    if sum(needs) <= total:
        return list(needs)
    budgets = [0] * len(needs)
    remaining = total
    order = sorted(range(len(needs)), key=lambda index: needs[index])
    for position, index in enumerate(order):
        share = remaining // (len(order) - position)
        budgets[index] = min(needs[index], share)
        remaining -= budgets[index]
    return budgets


def _draft(
    job: FailedJob,
    raw: RawLog,
    counter: TokenCounter,
    config: ExcerptConfig,
    error_patterns: Sequence[re.Pattern[str]],
) -> _Draft:
    text, cut = tail_text(raw.text, config.max_bytes)
    sliced = slice_step(parse_log(text), job.step_started_at, job.step_completed_at)
    lines = clean_lines(sliced, error_patterns)
    order = _keep_order(lines, config)
    # Every line costs at least one token, so no more than max_tokens lines can ever be kept.
    # Cutting here bounds the redaction and token counting work on huge logs.
    order = order[: config.max_tokens]
    redacted = dict(redact_lines([line.text for line in lines], only=order))
    keep_order: list[_Candidate] = []
    for position in order:
        source = lines[position]
        result = redacted[position]
        line_text = result.text
        if len(line_text) > config.max_line_chars:
            line_text = line_text[: config.max_line_chars] + TRUNCATED_LINE_SUFFIX
        line = LogLine(number=source.number, text=line_text)
        keep_order.append(
            _Candidate(
                line=line,
                is_error=source.is_error,
                tokens=counter.count(render_line(line) + "\n"),
                redactions=result.count,
            )
        )
    return _Draft(job=job, truncated=raw.truncated or cut, keep_order=keep_order)


def _keep_order(lines: Sequence[CleanLine], config: ExcerptConfig) -> list[int]:
    """Positions of the windowed lines, most important first.

    Error lines and their ``±protected_context`` neighbours come first, newest first; then the
    rest of the window, newest first. Trimming keeps a prefix of this order, which cuts the oldest
    context first and the protected lines last (SPEC §7.3 step 7).
    """
    count = len(lines)
    errors = [position for position, line in enumerate(lines) if line.is_error]
    window: set[int] = set(range(max(0, count - config.tail_lines), count))
    protected: set[int] = set()
    for position in errors:
        window.update(_around(position, config.error_context, count))
        protected.update(_around(position, config.protected_context, count))
    first = sorted(protected, reverse=True)
    rest = sorted(window - protected, reverse=True)
    return first + rest


def _around(position: int, radius: int, count: int) -> range:
    return range(max(0, position - radius), min(count, position + radius + 1))


def _finish(draft: _Draft, budget: int) -> LogExcerpt:
    kept: list[_Candidate] = []
    used = 0
    for candidate in draft.keep_order:
        if used + candidate.tokens > budget:
            break
        kept.append(candidate)
        used += candidate.tokens
    kept.sort(key=lambda candidate: candidate.line.number)
    numbers = [candidate.line.number for candidate in kept]
    return LogExcerpt(
        job=draft.job.job,
        step_name=draft.job.step_name,
        lines=[candidate.line for candidate in kept],
        omitted=_gaps(numbers),
        error_lines=[candidate.line.number for candidate in kept if candidate.is_error],
        token_count=used,
        truncated=draft.truncated,
        redactions=sum(candidate.redactions for candidate in kept),
    )


def _gaps(numbers: Sequence[int]) -> list[tuple[int, int]]:
    """``(from, to)`` ranges dropped before the first kept line and between kept lines.

    Lines after the last kept one are not listed: the excerpt keeps the end of the step.
    """
    return [
        (previous + 1, current - 1)
        for previous, current in pairwise([0, *numbers])
        if current - previous > 1
    ]
