"""Error signature: normalised canonical text and its SHA-256 (SPEC §7.4, D-09).

Two failures with the same signature are "the same error": the exact-match cache (P2) uses the
hash and the similarity search (P2) embeds the text. ``normalize`` is deterministic and
idempotent; property tests in ``tests/unit/test_signature.py`` hold it to that.
"""

import hashlib
import re
from collections.abc import Sequence

from cicd_doctor.core.models import FailedJob, LogExcerpt, Signature
from cicd_doctor.core.redaction import PLACEHOLDER

MAX_ERROR_LINES = 5
MAX_ERROR_KIND_CHARS = 80
ERROR_MARKER = "##[error]"

_A = re.ASCII  # \d, \w, \s and \b mean their ASCII versions: the same on every input
_WHITESPACE = re.compile(r"\s+", _A)
_URL_QUERY = re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s?#]+)\?[^\s#]*", _A)
_DATETIME = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?(?!\w)",
    _A,
)
_TIME = re.compile(r"\b\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?\b", _A)
_DURATION = re.compile(
    r"\b\d+h\s?\d+m(?:\s?\d+(?:\.\d+)?s)?\b"  # 1h 2m, 1h2m3s
    r"|\b\d+m\s?\d+(?:\.\d+)?s\b"  # 2m 30s, 1m3.5s
    r"|\b\d+(?:\.\d+)?\s?(?:ns|us|ms|s|secs?|seconds?|mins?|minutes?|h|hrs?|hours?)\b",
    _A,
)
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", _A
)
_HEX = re.compile(r"\b0x[0-9a-fA-F]+\b|\b[0-9a-fA-F]{7,}\b", _A)
# Temporary directories, whole path included. Checked before the workspace, which contains _temp.
_TEMP_PATH = re.compile(
    r"(?:/home/runner/work/_temp|[A-Za-z]:\\a\\_temp|/private/var/folders|/var/folders|/tmp"
    r"|[A-Za-z]:\\Users\\[^\\\s]+\\AppData\\Local\\Temp|%TEMP%|%TMP%|\$RUNNER_TEMP|\$TMPDIR)"
    r"(?=[/\\\s'\":,;)\]]|$)(?:[/\\][^\s'\":,;)\]]*)?",
    _A,
)
# GitHub-hosted workspaces: <work>/<repo>/<repo>/<path> becomes the repository-relative <path>,
# with "/" separators, so Linux, macOS and Windows runs of one error agree (D-59).
_WORKSPACE = re.compile(
    r"(?:/home/runner/work|/Users/runner/work|[A-Za-z]:\\a)[/\\][^/\\\s]+[/\\][^/\\\s]+[/\\]"
    r"([^\s'\":,;)\]]*)",
    _A,
)
_IPV6 = re.compile(
    r"(?<![\w:])(?:"
    r"(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}"
    r"|(?:[0-9a-fA-F]{1,4}:){1,7}:(?:[0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{1,4}){0,6})?"
    r"|::(?:[0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{1,4}){0,6})?"
    r")(?![\w:])",
    _A,
)
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", _A)
_PORT = re.compile(r"(<ip>\]?|\blocalhost|//[^/\s:@?#<>]+):\d{1,5}\b", _A)
_INTEGER = re.compile(r"(?<!\w)\d+(?!\w)", _A)  # digits inside identifiers (py3, x64) stay

_EXCEPTION_CLASS = re.compile(
    r"\b(?:[A-Za-z_][A-Za-z0-9_]*\.)*[A-Z][A-Za-z0-9_]*(?:Error|Exception)\b", _A
)
_MATRIX_SUFFIX = re.compile(r"\s*\([^()]*\)\s*$")


def normalize(line: str) -> str:
    """Replace the parts of a log line that change between runs of the same error (§7.4).

    Redaction placeholders are kept as they are. Case is preserved. The rules are applied until
    nothing changes, which makes the function idempotent by construction: one pass can uncover a
    new match, for example a second workspace path glued to the first.
    """
    for _ in range(_MAX_PASSES):
        normalized = _normalize_once(line)
        if normalized == line:
            break
        line = normalized
    return line


_MAX_PASSES = 8  # every pass shortens the text or replaces values with placeholders


def _normalize_once(line: str) -> str:
    parts = PLACEHOLDER.split(line)
    placeholders = PLACEHOLDER.findall(line)
    out: list[str] = []
    for index, part in enumerate(parts):
        out.append(_normalize_segment(part))
        if index < len(placeholders):
            out.append(placeholders[index])
    return _WHITESPACE.sub(" ", "".join(out)).strip()


def _normalize_segment(text: str) -> str:
    text = _WHITESPACE.sub(" ", text)
    text = _URL_QUERY.sub(r"\1", text)
    text = _DATETIME.sub("<t>", text)
    text = _TIME.sub("<t>", text)
    text = _DURATION.sub("<t>", text)
    text = _UUID.sub("<uuid>", text)
    text = _HEX.sub("<hex>", text)
    text = _TEMP_PATH.sub("<tmp>", text)
    text = _WORKSPACE.sub(lambda match: match.group(1).replace("\\", "/"), text)
    text = _IPV6.sub("<ip>", text)
    text = _IPV4.sub("<ip>", text)
    text = _PORT.sub(r"\1:<port>", text)
    return _INTEGER.sub("<n>", text)


def normalize_job_name(name: str) -> str:
    """Drop the matrix suffix: ``test (ubuntu-latest, 3.12)`` becomes ``test``."""
    return _WHITESPACE.sub(" ", _MATRIX_SUFFIX.sub("", name)).strip()


def error_type(error_texts: Sequence[str], top_rule_id: str | None = None) -> str:
    """The top rule id; else the first exception class in the error lines; else the kind of the
    first ``##[error]`` line (its normalised message up to the first colon); else ``-``."""
    if top_rule_id:
        return top_rule_id
    for text in error_texts:
        match = _EXCEPTION_CLASS.search(text)
        if match:
            return match.group(0)
    for text in error_texts:
        if ERROR_MARKER in text:
            message = normalize(text.split(ERROR_MARKER, 1)[1]).split(":", 1)[0].strip()
            return f"{ERROR_MARKER}{message[:MAX_ERROR_KIND_CHARS]}"
    return "-"


def canonical_text(job: FailedJob, excerpt: LogExcerpt, top_rule_id: str | None = None) -> str:
    """The canonical text of §7.4: workflow, job, step, error type and the first error lines.

    When the excerpt has no error line, its last non-blank lines stand in for them (D-59), so
    two different failures without error lines do not collapse into one signature.
    """
    by_number = {line.number: line.text for line in excerpt.lines}
    error_texts = [by_number[number] for number in excerpt.error_lines if number in by_number]
    if not error_texts:
        error_texts = [line.text for line in excerpt.lines if line.text.strip()][-MAX_ERROR_LINES:]
    normalized = [normalize(text) for text in error_texts]
    lines = [
        _WHITESPACE.sub(" ", job.job.run.workflow_name).strip(),
        normalize_job_name(job.job.job_name),
        _WHITESPACE.sub(" ", job.step_name).strip() if job.step_name else "-",
        error_type(error_texts, top_rule_id),
        *[text for text in normalized if text][:MAX_ERROR_LINES],
    ]
    return "\n".join(lines)


def compute_signature(
    job: FailedJob, excerpt: LogExcerpt, top_rule_id: str | None = None
) -> Signature:
    text = canonical_text(job, excerpt, top_rule_id)
    return Signature(text=text, hash=hashlib.sha256(text.encode("utf-8")).hexdigest())
