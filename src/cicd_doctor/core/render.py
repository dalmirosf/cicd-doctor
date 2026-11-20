"""Safe markdown for the check run and the sticky comment (SPEC §7.5, §9.5, D-25, D-64).

Every piece of text in a diagnosis was derived from an untrusted log, the rules diagnosis
included (its templates fill in values captured from the log). So all of it is rendered as inert
plain text: HTML is escaped, markdown that could make a link, an image, a heading or a table is
escaped, ``@mentions`` and issue references are neutralised, and a URL is shown as inline code
unless it is an ``https`` URL on a short allowlist. Secrets are redacted once more and every field
is cut to its maximum length. A successful prompt injection can then, at worst, produce a wrong
diagnosis: never a clickable phishing link, a notification, or markup that changes the page.
"""

import re
import unicodedata
from collections.abc import Sequence
from typing import NamedTuple
from urllib.parse import urlsplit

from cicd_doctor.core.logs.clean import strip_terminal_controls
from cicd_doctor.core.models import (
    CheckOutput,
    Diagnosis,
    DiagnosisReport,
    Failure,
    FailureStatus,
    FallbackReason,
    SimilarFailure,
)
from cicd_doctor.core.redaction import redact

HIGH_CONFIDENCE = 0.75  # SPEC §5.1
MEDIUM_CONFIDENCE = 0.45
MAX_SUMMARY_CHARS = 60_000  # the Checks API limit for a check run summary
MAX_COMMENT_CHARS = 20_000  # well below GitHub's 65,536 for a comment
COMMENT_EVIDENCE_LINES = 3
COMMENT_FIX_CHARS = 500
CHECK_NAME_PREFIX = "CICD Doctor"
CHECK_NAME_WORKFLOW_CHARS = 60
MAX_CHECK_NAME_CHARS = 80
FOOTER = "Diagnosis generated automatically from untrusted logs; verify before acting."

ALLOWED_HOSTS = frozenset(
    {
        "github.com",
        "docs.github.com",
        "docs.python.org",
        "pypi.org",
        "docs.docker.com",
        "registry.terraform.io",
        "docs.aws.amazon.com",
    }
)

FALLBACK_TEXT: dict[FallbackReason, str] = {
    "fork_policy": "LLM not used for fork PRs",
    "budget": "LLM daily limit reached",
    "config_off": "LLM disabled by the repository configuration",
    "on_demand": "LLM on demand: comment `/cicd-doctor explain`",
    "disabled": "LLM disabled",
    "llm_error": "LLM unavailable, showing the rule-based diagnosis",
    "invalid_output": "LLM answer was invalid, showing the rule-based diagnosis",
}

COULD_NOT_DIAGNOSE: dict[str, str] = {
    "logs_expired": "The logs of the failed job are no longer available (GitHub deletes them).",
    "no_failed_job": "No failed job was found in this run attempt.",
    "permission": "The app is not allowed to read the logs of this run.",
    "internal_error": "An internal error stopped the analysis.",
}

# Text that can hide or reorder other text: zero-width characters, bidirectional overrides.
_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿]")
# A scheme or a www. prefix starts a URL even with nothing after it: it never stays bare text.
# It has no "must follow a space" condition: over-matching only turns more text into code.
_URL = re.compile(r"(?i)(?:[a-z][a-z0-9+.\-]*://|www\.)\S*")
_URL_TRAILING = ".,;:!?)]}'\""
# #123, GH-123 and owner/repo#123 would become links to issues; they are shown as code instead.
# They are matched anywhere, with no word-boundary conditions: over-matching only makes code.
_REFERENCE = re.compile(r"(?:[\w.\-]+/[\w.\-]+)?#\d+|(?i:GH-\d+)")
_ESCAPED = re.compile(r"([\\*_\[\]|~`])")
_LEADING = re.compile(r"^(\s*)([#>+\-=]|\d+[.)])")  # heading, quote, list, setext underline
_ASCII_NAME = re.compile(r"[^A-Za-z0-9._:/@+\- ]")
_REPO = re.compile(r"[A-Za-z0-9._\-]+/[A-Za-z0-9._\-]+")
_COMMIT_SHA = re.compile(r"[0-9a-f]{7,40}")
_FULL_SHA = re.compile(r"[0-9a-f]{40}")
FLAKY_BANNER = "✅ Passed on re-run without code changes — recorded as flaky"
MAX_CHECK_SIMILAR = 3
MAX_COMMENT_SIMILAR = 1
ZWJ = "‍"
MAX_URL_CODE_CHARS = 200


# --- text -------------------------------------------------------------------------------------


def flatten(text: str, limit: int) -> str:
    """Redact secrets, drop control and invisible characters, make one line, cut to ``limit``."""
    clean = _INVISIBLE.sub("", strip_terminal_controls(redact(text)))
    # Every format character (Cf) is invisible too: the soft hyphen, for one, is not in the list.
    clean = "".join(char for char in clean if unicodedata.category(char) != "Cf")
    return " ".join(clean.split())[:limit]


def inline(text: str, limit: int) -> str:
    """One line of untrusted prose as safe markdown.

    Backtick pairs in the text become code spans (the rule templates use them for names); an odd
    number of backticks means the text is not trying to be code, and they are all escaped.
    """
    flat = flatten(text, limit)
    parts = flat.split("`")
    if len(parts) % 2 == 0:
        return _prose("`".join(parts))  # _escape turns every backtick into a literal one
    out: list[str] = []
    for index, part in enumerate(parts):
        if index % 2:
            if part.strip():
                out.append(f"`{part}`")
        else:
            out.append(_prose(part))
    return "".join(out).strip()


def code(text: str, limit: int) -> str:
    """Untrusted text as one inline code span. It cannot end the span: backticks are removed."""
    flat = flatten(text, limit).replace("`", "'")
    return f"`{flat}`" if flat else ""


def fenced(lines: Sequence[str], language: str = "text") -> str:
    """A fenced code block longer than any backtick run in its content, so it cannot be closed."""
    body = "\n".join(lines)
    longest = max((len(run) for run in re.findall(r"`+", body)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{body}\n{fence}"


def allowed_url(url: str) -> bool:
    """True for an ``https`` URL whose host is exactly on the allowlist: no credentials, no port,
    no odd characters (SPEC §9.5)."""
    if len(url) > 500 or re.search(r"[^\w\-./~:?#@!$&'()*+,;=%]", url):
        return False
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and host in ALLOWED_HOSTS
        and port is None
        and "@" not in parts.netloc
        and "\\" not in url
    )


def _prose(text: str) -> str:
    out: list[str] = []
    cursor = 0
    for match in _URL.finditer(text):
        out.append(_references(text[cursor : match.start()]))
        url = match.group(0).rstrip(_URL_TRAILING)
        trailing = match.group(0)[len(url) :]
        out.append(f"<{url}>" if allowed_url(url) else code(url[:MAX_URL_CODE_CHARS], 500))
        out.append(_references(trailing))
        cursor = match.end()
    out.append(_references(text[cursor:]))
    return _escape_leading("".join(out))


def _references(text: str) -> str:
    out: list[str] = []
    cursor = 0
    for match in _REFERENCE.finditer(text):
        out.append(_escape(text[cursor : match.start()]))
        out.append(f"`{match.group(0)}`")
        cursor = match.end()
    out.append(_escape(text[cursor:]))
    return "".join(out)


def _escape(text: str) -> str:
    """HTML-escape, neutralise mentions, escape markdown. ``&`` goes first: an entity such as
    ``&#64;user`` would otherwise render as ``@user`` and notify."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("@", "@" + ZWJ)
    return _ESCAPED.sub(r"\\\1", text)


def _escape_leading(text: str) -> str:
    return _LEADING.sub(lambda m: f"{m.group(1)}\\{m.group(2)}", text, count=1)


# --- the pieces of a report -------------------------------------------------------------------


def confidence_label(confidence: float) -> str:
    if confidence >= HIGH_CONFIDENCE:
        return "high"
    return "medium" if confidence >= MEDIUM_CONFIDENCE else "low"


def check_name(workflow_name: str) -> str:
    """``CICD Doctor: <workflow>`` for the name of the check run (D-76).

    The workflow name is untrusted: it is flattened (secrets redacted, control, invisible and bidi
    characters dropped, one line) and cut to 60 characters, with ``…`` when it was cut. The Checks
    API takes plain text, so there is no markdown escaping. Nothing left gives ``CICD Doctor``.
    """
    flat = flatten(workflow_name, 10_000)
    if len(flat) > CHECK_NAME_WORKFLOW_CHARS:
        flat = flat[: CHECK_NAME_WORKFLOW_CHARS - 1].rstrip() + "…"
    if not flat:
        return CHECK_NAME_PREFIX
    return f"{CHECK_NAME_PREFIX}: {flat}"[:MAX_CHECK_NAME_CHARS]


class _Banner(NamedTuple):
    """How a resolved failure is announced (D-84): plain text for the title of a check run,
    markdown for a comment, and the link that compares the two commits when there is one."""

    plain: str
    markdown: str
    compare: str | None


def resolution_banner(report: DiagnosisReport) -> _Banner | None:
    """The banner of a report whose failures were all resolved the same way, else None.

    The text is built here from the status and from a commit sha that must be 40 hex digits (shown
    as seven): nothing of the stored diagnosis, and no URL that came from a log. The comparison
    link is derived from the repository name and the two shas, checked like every other link
    (D-64); a sha that does not look like one is left out, with its link.
    """
    if not report.items:
        return None
    statuses = {(f.status, f.fix_commit_sha) for f, _ in report.items}
    if len(statuses) != 1:
        return None
    status, fix_sha = next(iter(statuses))
    if status is FailureStatus.RESOLVED_FLAKY:
        return _Banner(FLAKY_BANNER, FLAKY_BANNER, None)
    if status is not FailureStatus.RESOLVED_BY_COMMIT:
        return None
    if fix_sha is None or not _FULL_SHA.fullmatch(fix_sha):
        text = "✅ Fixed by a later commit"
        return _Banner(text, text, None)
    failing = report.items[0][0].job.job.run
    compare = None
    if _REPO.fullmatch(failing.repo_full_name) and _FULL_SHA.fullmatch(failing.head_sha):
        compare = (
            f"https://github.com/{failing.repo_full_name}/compare/{failing.head_sha}...{fix_sha}"
        )
    return _Banner(f"✅ Fixed by {fix_sha[:7]}", f"✅ Fixed by `{fix_sha[:7]}`", compare)


def _banner_lines(banner: _Banner, *, bold: bool) -> str:
    text = f"**{banner.markdown}**" if bold else banner.markdown
    if banner.compare is None:
        return text
    return f"{text}\n\n[Compare the changes]({banner.compare})"


def render_title(report: DiagnosisReport) -> str:
    """``Likely cause: dependency (high confidence)`` for the most confident job, or the plain
    banner when every failure of the report was resolved."""
    if not report.items:
        return "Could not diagnose"
    banner = resolution_banner(report)
    if banner is not None:
        return banner.plain
    _, best = max(report.items, key=lambda item: item[1].confidence)
    title = f"Likely cause: {best.category.value} ({confidence_label(best.confidence)} confidence)"
    return title + (f" (+{len(report.items) - 1} more)" if len(report.items) > 1 else "")


def render_check(report: DiagnosisReport) -> CheckOutput:
    """The title and the markdown summary of the check run: one section per failed job."""
    sections = [_job_section(report, failure, diagnosis) for failure, diagnosis in report.items]
    if not sections:
        sections = ["No failed job was found in this run attempt."]
    banner = resolution_banner(report)
    if banner is not None:
        sections.insert(0, _banner_lines(banner, bold=False))
    if report.config_problems:
        fields = ", ".join(code(problem, 60) for problem in report.config_problems)
        sections.append(f"_The repository configuration is invalid: {fields}; LLM turned off._")
    return CheckOutput(title=render_title(report), summary=_join(sections, MAX_SUMMARY_CHARS))


def render_comment(report: DiagnosisReport) -> str:
    """The sticky PR comment: the same content, shorter, starting with the hidden marker."""
    run = report.run
    marker = f"<!-- cicd-doctor:v1 pr={int(run.pr_number or 0)} workflow={int(run.workflow_id)} -->"
    workflow = inline(run.workflow_name, CHECK_NAME_WORKFLOW_CHARS)
    heading = f"### CICD Doctor: {workflow}" if workflow else "### CICD Doctor"
    title = render_title(report)
    attempt = f"Attempt {int(run.run_attempt)}" if run.run_attempt > 1 else ""
    body = [
        _job_section(report, failure, diagnosis, compact=True)
        for failure, diagnosis in report.items
    ]
    banner = resolution_banner(report)
    lead = _banner_lines(banner, bold=True) if banner is not None else f"**{title}**"
    head = "\n".join([marker, heading, lead] + ([attempt] if attempt else []))
    return _join([head, *body], MAX_COMMENT_CHARS)


def render_could_not_diagnose(reason: str) -> CheckOutput:
    """A neutral message for a run that cannot be analysed. ``reason`` is a key of
    ``COULD_NOT_DIAGNOSE``; anything else gets the generic text, never echoed."""
    text = COULD_NOT_DIAGNOSE.get(reason, COULD_NOT_DIAGNOSE["internal_error"])
    return CheckOutput(title="Could not diagnose", summary=f"{text}\n\n---\n_{FOOTER}_")


def _join(sections: Sequence[str], limit: int) -> str:
    """The sections and the footer, within ``limit`` characters (whole sections only)."""
    footer = f"---\n_{FOOTER}_"
    budget = limit - len(footer) - 100
    kept: list[str] = []
    used = 0
    for index, section in enumerate(sections):
        if used + len(section) + 2 <= budget:
            kept.append(section)
            used += len(section) + 2
            continue
        if not kept:
            kept.append("_The diagnosis is too long to show._")
        elif len(sections) - index:
            kept.append(f"_{len(sections) - index} more section(s) not shown._")
        break
    return "\n\n".join([*kept, footer])


def _job_section(
    report: DiagnosisReport, failure: Failure, diagnosis: Diagnosis, *, compact: bool = False
) -> str:
    job = failure.job
    lines = [f"**Job:** {inline(job.job.job_name, 120)}" + _step(failure)]
    if failure.excerpt.truncated:
        lines.append("_The log was longer than 20 MB: only its end was read._")
    lines.append(f"**Probable cause:** {inline(diagnosis.probable_cause, 500)}")
    evidence = _evidence(failure, diagnosis, COMMENT_EVIDENCE_LINES if compact else None)
    if evidence:
        lines.append(evidence)
    elif not diagnosis.evidence_verified:
        lines.append("_No line of the log supports this diagnosis._")
    fix_limit = COMMENT_FIX_CHARS if compact else 1000
    lines.append(f"**Suggested fix:** {inline(diagnosis.suggested_fix, fix_limit)}")
    lines.append(_source(diagnosis, report.cached_from_runs.get(failure.id)))
    baseline = report.rules_baseline.get(failure.id)
    if baseline is not None:
        lines.append(_baseline(baseline))
    similar = report.similar.get(failure.id, [])
    if similar:
        # The check run lists up to three earlier runs; the comment, one line (D-82).
        lines.append(
            _similar(
                similar,
                job.job.run.repo_full_name,
                MAX_COMMENT_SIMILAR if compact else MAX_CHECK_SIMILAR,
            )
        )
    return "\n\n".join(lines)


def _step(failure: Failure) -> str:
    job = failure.job
    if not job.step_name:
        return ""
    label = f" · **Step:** {inline(job.step_name, 120)}"
    repo = job.job.run.repo_full_name
    if job.step_number is not None and _REPO.fullmatch(repo):
        run = job.job.run
        url = (
            f"https://github.com/{repo}/actions/runs/{int(run.run_id)}"
            f"/job/{int(job.job.job_id)}#step:{int(job.step_number)}:1"
        )
        label += f" ([open the step]({url}))"
    return label


def _evidence(failure: Failure, diagnosis: Diagnosis, limit: int | None) -> str:
    items = diagnosis.evidence[:limit]
    if not items:
        return ""
    # Line numbers are positions in the retained log, not GitHub's per-step numbers (D-64), so
    # they are shown as labels and never turned into links.
    shown = [f"L{int(item.line)}: {flatten(item.text, 300)}" for item in items]
    return "**Evidence:**\n" + fenced(shown)


def _source(diagnosis: Diagnosis, origin_run: int | None = None) -> str:
    if diagnosis.source == "llm":
        model = _ASCII_NAME.sub("", diagnosis.model or "")[:60] or "unknown model"
        version = _ASCII_NAME.sub("", diagnosis.prompt_version or "")[:20]
        text = f"LLM {model} · prompt {version}".rstrip()
    elif diagnosis.source == "cache":
        # The origin is an int from the store, never a stored string (D-64).
        text = (
            "cached from an earlier run"
            if origin_run is None
            else f"cached from run #{int(origin_run)}"
        )
    else:
        ids = ", ".join(_ASCII_NAME.sub("", rule_id)[:30] for rule_id in diagnosis.rule_ids[:1])
        text = f"rule {ids}" if ids else "rules (no rule matched)"
    line = f"**Source:** {code(text, 120)} · confidence {confidence_label(diagnosis.confidence)}"
    line += f" ({diagnosis.confidence:.2f})"
    if diagnosis.fallback_reason:
        line += f" · {FALLBACK_TEXT[diagnosis.fallback_reason]}"
    return line


def _baseline(rules: Diagnosis) -> str:
    ids = ", ".join(_ASCII_NAME.sub("", rule_id)[:30] for rule_id in rules.rule_ids[:1])
    return (
        f"**Rule baseline disagrees:** {code(ids or 'rules', 60)} says "
        f"`{rules.category.value}` (confidence {rules.confidence:.2f}): "
        f"{inline(rules.probable_cause, 300)}"
    )


def _similar(similar: Sequence[SimilarFailure], repo: str, limit: int) -> str:
    """The earlier runs that looked like this one (D-82).

    The link to a run is built here from integers and a repository name checked like the one of
    the step link, never from a stored string (D-64). The cause is untrusted prose and the commit
    is shown as code. Without a valid repository name the run is shown without a link.
    """
    rows = []
    for item in similar[:limit]:
        run_id = int(item.run_id)
        label = f"run #{run_id}"
        ref = (
            f"[{label}](https://github.com/{repo}/actions/runs/{run_id})"
            if _REPO.fullmatch(repo)
            else label
        )
        sha = item.fix_commit_sha
        fixed = f", fixed by {code(sha, 12)}" if sha and _COMMIT_SHA.fullmatch(sha) else ""
        cause = inline(item.probable_cause, 200)
        rows.append(f"- {ref} · `{item.category.value}` (score {item.score:.2f}): {cause}{fixed}")
    heading = "**Similar past failure:**" if limit == 1 else "**Similar past failures:**"
    return heading + "\n" + "\n".join(rows)
