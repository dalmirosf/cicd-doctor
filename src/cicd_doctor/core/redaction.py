"""Secret redaction (SPEC §9.2, D-12, D-59).

Matches are replaced with ``[REDACTED:<kind>]``; only counts are reported, never the values.
Redaction runs on the excerpt (before storage or the LLM), on the LLM output (before publishing)
and on job errors (``jobs.last_error``). It is idempotent: redacting redacted text changes
nothing and counts nothing.

Not redacted by shape, on purpose: 40-char git SHAs, UUIDs and GitHub's own ``***`` masks. A
value assigned to a secret-named key (``token=...``) is redacted whatever its shape, because
some real tokens are UUIDs or hex strings (D-59).
"""

import math
import re
from collections import Counter
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass

PLACEHOLDER_PREFIX = "[REDACTED:"
PLACEHOLDER = re.compile(r"\[REDACTED:[a-z_]+\]")

GENERIC_MIN_ENTROPY = 3.5  # bits per character
GENERIC_ENTROPY_SLACK = 1.0  # short values: within this many bits of the maximum for the length
GENERIC_MIN_LENGTH = 20  # a value this long is redacted whatever its entropy

# A value that is already a placeholder, or GitHub's own mask for a registered secret, is not
# redacted again: the secret is not in the log. The checkout step of every real job prints
# "AUTHORIZATION: basic ***", which used to be reported as a redaction on every log.
_NOT_MASKED = r"(?!\[REDACTED:|\*\*\*)"

# A line can only hold a secret if it contains one of these (case-insensitive) fragments.
# Checking this first keeps redaction cheap on the many lines that hold none.
_TRIGGER = re.compile(
    r"(?i)-----BEGIN|://|authoriz|aws_secret|gh[pousr]_|github_pat_|AKIA|ASIA|AIza|ya29\."
    r"|sk-|k_live_|xox[abpr]-|eyJ|token|secret|passw|pwd|api[_-]?key|access[_-]?key"
)

_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY[A-Z ]*-----")
_PEM_END = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY[A-Z ]*-----")
# BEGIN ... END on one line (also JSON with literal "\n" escapes), or BEGIN to the end of the line.
_PEM_INLINE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY[A-Z ]*-----.*?"
    r"(?:-----END [A-Z0-9 ]*PRIVATE KEY[A-Z ]*-----|$)"
)
# Lines that can appear inside a PEM block: base64, encryption headers, blank lines.
_PEM_BODY = re.compile(r"^\s*(?:[A-Za-z0-9+/=]+|Proc-Type:.*|DEK-Info:.*)?\s*$")


@dataclass(frozen=True, slots=True)
class _Rule:
    kind: str
    pattern: re.Pattern[str]
    # Replacement for one match: the full replacement text, or None to leave the match as is.
    replace: Callable[[re.Match[str], str], str | None]


def _whole(match: re.Match[str], placeholder: str) -> str:
    return placeholder


def _keep_prefix(match: re.Match[str], placeholder: str) -> str:
    """Keep group 1 (the key or header name), replace the value."""
    return match.group(1) + placeholder


def _url_credentials(match: re.Match[str], placeholder: str) -> str:
    return f"{match.group(1)}{placeholder}@"


def _generic(match: re.Match[str], placeholder: str) -> str | None:
    value = match.group(3)
    if not _looks_secret(value):
        return None
    return match.group(1) + match.group(2) + placeholder


def _looks_secret(value: str) -> bool:
    if value.startswith(PLACEHOLDER_PREFIX) or "***" in value:
        return False
    if value.startswith(("$", "%", "/", "~")) or re.match(r"[A-Za-z]:\\", value):
        return False  # a variable reference or a filesystem path, not a value
    return len(value) >= GENERIC_MIN_LENGTH or shannon_entropy(value) >= entropy_threshold(
        len(value)
    )


def entropy_threshold(length: int) -> float:
    """Entropy a value of ``length`` chars needs to count as a secret.

    A string of n chars has at most log2(n) bits per char, so a fixed 3.5 bits could never match
    a value shorter than 12 chars. Short values need to be within ``GENERIC_ENTROPY_SLACK`` bits
    of that maximum instead (D-59).
    """
    return min(GENERIC_MIN_ENTROPY, math.log2(length) - GENERIC_ENTROPY_SLACK)


def shannon_entropy(value: str) -> float:
    """Shannon entropy of ``value`` in bits per character."""
    if not value:
        return 0.0
    total = len(value)
    return -sum(n / total * math.log2(n / total) for n in Counter(value).values())


# Structural rules first (they contain token-shaped values), generic assignments last.
_RULES: tuple[_Rule, ...] = (
    _Rule("private_key", _PEM_INLINE, _whole),
    _Rule(
        "url_credentials",
        re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://)[^/\s:@\[]+:(?!\*\*\*@)[^/\s@]+@"),
        _url_credentials,
    ),
    _Rule(
        "authorization",
        re.compile(rf"(?i)(authorization:\s*(?:bearer|basic|token)\s+){_NOT_MASKED}\S+"),
        _keep_prefix,
    ),
    _Rule(
        "aws_secret_access_key",
        re.compile(rf"(?i)(aws_secret_access_key['\"]?\s*[=:]\s*['\"]?){_NOT_MASKED}[^\s'\"]+"),
        _keep_prefix,
    ),
    _Rule("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}"), _whole),
    _Rule("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}"), _whole),
    _Rule("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), _whole),
    _Rule("google_api_key", re.compile(r"\bAIza[0-9A-Za-z\-_]{35,}"), _whole),
    _Rule("google_oauth_token", re.compile(r"\bya29\.[0-9A-Za-z\-_]+"), _whole),
    _Rule("anthropic_api_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"), _whole),
    _Rule("openai_api_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"), _whole),
    _Rule("slack_token", re.compile(r"\bxox[abpr]-[A-Za-z0-9\-]{10,}"), _whole),
    _Rule("stripe_key", re.compile(r"\b[sr]k_live_[A-Za-z0-9]{16,}"), _whole),
    _Rule(
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
        _whole,
    ),
    _Rule(
        "secret",
        re.compile(
            r"(?i)(token|secret|password|passwd|pwd|api[_-]?key|access[_-]?key|client[_-]?secret)"
            r"(['\"]?\s*[=:]\s*['\"]?)"
            r"([^\s'\"]{8,})"
        ),
        _generic,
    ),
)

KINDS: tuple[str, ...] = tuple(rule.kind for rule in _RULES)


def placeholder(kind: str) -> str:
    return f"{PLACEHOLDER_PREFIX}{kind}]"


@dataclass(frozen=True, slots=True)
class Redacted:
    text: str
    count: int  # number of replacements; never the values


def redact_line(line: str) -> Redacted:
    """Redact one line (no newlines). Multi-line PEM blocks need ``redact_lines``."""
    if not _TRIGGER.search(line):
        return Redacted(line, 0)
    count = 0
    for rule in _RULES:
        marker = placeholder(rule.kind)

        def substitute(match: re.Match[str], rule: _Rule = rule, marker: str = marker) -> str:
            nonlocal count
            replacement = rule.replace(match, marker)
            if replacement is None:
                return match.group(0)
            count += 1
            return replacement

        line = rule.pattern.sub(substitute, line)
    return Redacted(line, count)


def redact_lines(
    lines: Sequence[str], only: Collection[int] | None = None
) -> list[tuple[int, Redacted]]:
    """Redact ``lines`` (indices into the sequence), handling PEM blocks that span lines.

    PEM blocks are found over *all* lines, so a selected line in the middle of a key is redacted
    even when its ``BEGIN`` line is not selected. Returns ``(index, Redacted)`` for each selected
    index (all of them when ``only`` is None), in ascending order. Each line of a multi-line PEM
    block counts as one replacement.
    """
    selected = range(len(lines)) if only is None else sorted(i for i in only if 0 <= i < len(lines))
    pem = _pem_block_roles(lines)
    marker = placeholder("private_key")
    result: list[tuple[int, Redacted]] = []
    for index in selected:
        role = pem.get(index)
        line = lines[index]
        if role is None:
            result.append((index, redact_line(line)))
        elif role == "begin":
            begin = _PEM_BEGIN.search(line)
            assert begin is not None
            prefix = redact_line(line[: begin.start()])
            result.append((index, Redacted(prefix.text + marker, prefix.count + 1)))
        elif role == "end":
            end = _PEM_END.search(line)
            assert end is not None
            suffix = redact_line(line[end.end() :])
            result.append((index, Redacted(marker + suffix.text, suffix.count + 1)))
        else:
            result.append((index, Redacted(marker, 1)))
    return result


def redact_text(text: str) -> Redacted:
    """Redact a whole text, line by line, including PEM blocks that span lines."""
    lines = text.split("\n")
    redacted = redact_lines(lines)
    return Redacted(
        "\n".join(item.text for _, item in redacted), sum(item.count for _, item in redacted)
    )


def redact(text: str) -> str:
    """Shorthand for ``redact_text(text).text``."""
    return redact_text(text).text


def _pem_block_roles(lines: Sequence[str]) -> dict[int, str]:
    """Index -> "begin" | "body" | "end" for every line inside a PEM block spanning lines.

    A block starts at a ``BEGIN ... PRIVATE KEY`` line without the matching ``END`` after it on
    the same line, and runs to the ``END`` line, or, when that never comes, while the lines look
    like PEM content (base64, headers, blank lines).
    """
    roles: dict[int, str] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        begin = _PEM_BEGIN.search(line) if "-----BEGIN" in line else None
        if begin is None or _PEM_END.search(line, begin.end()):
            index += 1
            continue
        roles[index] = "begin"
        index += 1
        while index < len(lines):
            line = lines[index]
            if "-----END" in line and _PEM_END.search(line):
                roles[index] = "end"
                index += 1
                break
            if not _PEM_BODY.match(line):
                break
            roles[index] = "body"
            index += 1
    return roles
