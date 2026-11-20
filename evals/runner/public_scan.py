"""Cleaning a third-party log for storage (D-101): the capture pass and the independent scanner.

``redact_text`` (D-59) is the redactor of production and it knows secrets. A public log is also a
log of other people: it can carry their e-mail addresses and their logins. ``capture_pass`` replaces
those, and ``scan`` is a second, independent look at the result. The scanner shares no code with
the redactor on purpose (the oracle must not call the code it judges): it has its own patterns,
and it is specific, because a naive one fires on every ``name@1.2.3`` and every package hash (the
spike measured it). A case is written only when ``scan`` finds nothing.

Nothing here returns or prints a matched value: findings carry the class and the line number only.
"""

import re
from dataclasses import dataclass

EMAIL_PLACEHOLDER = "[REDACTED:email]"
USER_PLACEHOLDER = "[user]"

# An e-mail needs a real top-level domain (letters), so ``name@1.2.3`` and ``pkg@2.0.0-beta.1``
# are not e-mails. A domain label cannot start or end with a hyphen.
_EMAIL = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9][A-Za-z0-9._%+-]*@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,24}(?![\w-])"
)
# ``actor: name``, ``GITHUB_ACTOR=name``: the value is a login.
_ACTOR = re.compile(
    r"(?i)\b(?P<key>(?:triggering[_-])?actor|github[_.]actor|github_triggering_actor)"
    r"(?P<sep>\s*[:=]\s*)(?P<value>[A-Za-z0-9][A-Za-z0-9-]{0,38})\b"
)
# ``Author: Jane Doe``, ``Co-authored-by: ...``: the rest of the line is a name (and an e-mail).
# ``@login`` mentions are not touched: ``@property`` and ``@pytest.fixture`` look the same.
_IDENTITY = re.compile(
    r"(?im)\b(?P<key>author|committer|co-authored-by|signed-off-by)(?P<sep>\s*:\s*)"
    r"(?!\[user\])(?P<value>\S.*)$"
)

# The scanner's own, looser e-mail pattern: it must not share the capture pass's regex, or a bug
# there would hide from here. The first label of the domain starts with a letter, so ``pkg@1.2.3``
# and ``lodash@4.17.21.tgz`` are not e-mails.
_SCAN_EMAIL = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
)

# The scanner's own patterns, by class. Prefixes of known token families (each with the length a
# real token has), a bearer or basic credential, a URL with a password, a PEM header.
_SCAN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", _SCAN_EMAIL),
    ("actor", re.compile(r"(?i)\bactor\s*[:=]\s*(?!\[user\])[A-Za-z0-9]")),
    (
        "identity",
        re.compile(r"(?i)\b(?:author|committer|co-authored-by|signed-off-by)\s*:\s*(?!\[user\])\S"),
    ),
    ("pem", re.compile(r"-----BEGIN [A-Z0-9 ]*(?:PRIVATE KEY|CERTIFICATE)[A-Z ]*-----")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}")),
    ("npm_token", re.compile(r"\bnpm_[A-Za-z0-9]{30,}")),
    ("pypi_token", re.compile(r"\bpypi-[A-Za-z0-9_-]{40,}")),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}\b")),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("stripe_key", re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("api_key", re.compile(r"\b(?:sk|pk)-[A-Za-z0-9_-]{24,}")),
    ("sendgrid_key", re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}")),
    ("docker_pat", re.compile(r"\bdckr_pat_[A-Za-z0-9_-]{20,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    (
        "credential",
        re.compile(
            r"(?i)\bauthorization:\s*(?:bearer|basic|token)\s+"
            r"(?!\*\*\*|\[REDACTED:)[A-Za-z0-9._~+/=-]{12,}"
        ),
    ),
    (
        "url_password",
        re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@\[]+:(?!\*\*\*|\[REDACTED:)[^/\s@\[]{3,}@"),
    ),
)


@dataclass(frozen=True, slots=True)
class Finding:
    kind: str
    line: int  # 1-based


@dataclass(frozen=True, slots=True)
class CapturePass:
    text: str
    emails: int
    logins: int


def capture_pass(text: str) -> CapturePass:
    """Replace e-mail addresses and logins: the personal data the production redactor ignores."""
    emails = 0
    logins = 0

    def email(_: re.Match[str]) -> str:
        nonlocal emails
        emails += 1
        return EMAIL_PLACEHOLDER

    def identity(match: re.Match[str]) -> str:
        nonlocal logins
        logins += 1
        return f"{match.group('key')}{match.group('sep')}{USER_PLACEHOLDER}"

    out = _EMAIL.sub(email, text)
    out = _IDENTITY.sub(identity, out)
    out = _ACTOR.sub(identity, out)
    return CapturePass(out, emails, logins)


def scan(text: str) -> list[Finding]:
    """What is left in ``text`` that must not be stored. Class and line only, never the value."""
    findings: list[Finding] = []
    for number, line in enumerate(text.split("\n"), start=1):
        for kind, pattern in _SCAN_PATTERNS:
            if pattern.search(line):
                findings.append(Finding(kind, number))
    return findings


def summarize(findings: list[Finding]) -> str:
    """A reason for the discard list: the classes and how many lines, no values, no text."""
    kinds = sorted({finding.kind for finding in findings})
    lines = len({finding.line for finding in findings})
    return f"scanner found {', '.join(kinds)} on {lines} line(s)"
