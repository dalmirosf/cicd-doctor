"""Load the rule fixtures in ``tests/fixtures/rules/<RULE-ID>.txt`` (SPEC §10).

A file holds sections that start with ``=== positive: <description>`` or
``=== negative: <description>``. A section may start with metadata lines:

- ``# step: <name>`` and ``# conclusion: failure|timed_out``: the failed job's context;
- ``# top: <RULE-ID>``: the rule expected to rank first, when it is not the file's rule;
- ``# category: <category>``: the category the file's rule must report;
- ``# expect-cause: <text>``: text the cause must contain (a captured value).

The other lines are the excerpt, numbered from 1. The fixtures are synthetic snippets shaped
like real logs; the lab captures check the rules against real runs.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from cicd_doctor.core.models import FailedJob, JobRef, LogExcerpt, LogLine
from tests.factories import make_run_ref

RULE_CASES_DIR = Path(__file__).parent / "fixtures" / "rules"
_METADATA = ("step", "conclusion", "top", "category", "expect-cause")
T0 = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)


@dataclass(frozen=True)
class RuleCase:
    rule_id: str
    kind: Literal["positive", "negative"]
    description: str
    lines: list[str]
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def case_id(self) -> str:
        return f"{self.rule_id}:{self.kind}:{self.description}"

    def job(self) -> FailedJob:
        conclusion = self.metadata.get("conclusion", "failure")
        assert conclusion in ("failure", "timed_out")
        return FailedJob(
            job=JobRef(run=make_run_ref(), job_id=1, job_name="lab"),
            conclusion="timed_out" if conclusion == "timed_out" else "failure",
            step_name=self.metadata.get("step"),
            step_number=3 if "step" in self.metadata else None,
            started_at=T0,
            completed_at=T0,
            runner_os="Linux",
        )

    def excerpt(self) -> LogExcerpt:
        return LogExcerpt(
            job=self.job().job,
            step_name=self.metadata.get("step"),
            lines=[LogLine(number=n, text=t) for n, t in enumerate(self.lines, start=1)],
            omitted=[],
            error_lines=[],
            token_count=0,
            truncated=False,
            redactions=0,
        )


def load_rule_cases() -> list[RuleCase]:
    cases: list[RuleCase] = []
    for path in sorted(RULE_CASES_DIR.glob("*.txt")):
        cases.extend(_parse(path.stem, path.read_text(encoding="utf-8")))
    return cases


def _parse(rule_id: str, text: str) -> list[RuleCase]:
    cases: list[RuleCase] = []
    header: str | None = None
    body: list[str] = []

    def flush() -> None:
        if header is None:
            assert not any(line.strip() for line in body), f"{rule_id}: text before a section"
            return
        kind, _, description = header.partition(":")
        assert kind.strip() in ("positive", "negative"), f"{rule_id}: bad section {header!r}"
        metadata: dict[str, str] = {}
        lines = list(body)
        while lines and not lines[-1]:
            lines.pop()
        while lines and lines[0].startswith("# ") and lines[0][2:].split(":")[0] in _METADATA:
            key, _, value = lines.pop(0)[2:].partition(":")
            metadata[key.strip()] = value.strip()
        assert lines, f"{rule_id}: empty section {header!r}"
        cases.append(
            RuleCase(rule_id, kind.strip(), description.strip(), lines, metadata)  # type: ignore[arg-type]
        )

    for line in text.split("\n"):
        if line.startswith("=== "):
            flush()
            header, body = line[4:], []
        else:
            body.append(line)
    flush()
    return cases
