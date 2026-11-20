"""What ``discover`` and ``capture-public`` share (D-101): the licence list, the API budget and the
helpers that keep third-party text out of the output.

Everything read from GitHub about a public repository is third-party data, and so is every log: a
workflow name, a branch name or a job name can carry text meant for a model or for a terminal. It is
cleaned before it is stored and it is never printed; the output of both commands holds counts, ids
and reasons from a fixed set.
"""

import json
import re
from collections.abc import Sequence
from typing import Any

from cicd_doctor.core.suggestions import has_hidden_controls
from evals.runner.capture import CaptureError, Gh

# The licences a case may come from (D-101): permissive, with attribution (kept in NOTICE).
ALLOWED_LICENSES: frozenset[str] = frozenset(
    {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "MPL-2.0"}
)

# Workflows that are gates or bots and not builds (measured by the spike). Matched as a
# case-insensitive substring of the workflow name.
DEFAULT_WORKFLOW_EXCLUSIONS: tuple[str, ...] = (
    "checklist",
    "triage",
    "review",
    "preview",
    "scanning",
    "zizmor",
    "disclosure",
    "merge",
    "publish",
    "release",
    "semantic",
    "documentation",
    "autofix",
    "size",
    "dependabot",
)

MAX_API_CALLS = 400
MIN_RATE_REMAINING = 200  # stop when GitHub says this many calls are left
RATE_PROBE_EVERY = 20  # calls between two looks at the rate limit

MAX_LOG_BYTES = 150 * 1024
MAX_LOG_LINES = 1500
MAX_FIX_JSON_BYTES = 2048

_HTTP_GONE = re.compile(r"HTTP (?:404|410)\b|Not Found|\bGone\b")
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class BudgetExhaustedError(Exception):
    """The API budget is spent (``--max-api-calls`` or the rate limit): stop, keep what is done."""


class BudgetedGh:
    """A ``Gh`` that counts its calls and watches the rate limit.

    Before every ``probe_every`` calls it reads ``GET /rate_limit`` (which GitHub does not count
    against the limit: it returns the same numbers as the ``x-ratelimit-remaining`` header, in a
    body ``gh`` hands over without parsing headers) and stops when ``min_remaining`` are left.
    """

    def __init__(
        self,
        gh: Gh,
        *,
        max_calls: int = MAX_API_CALLS,
        min_remaining: int = MIN_RATE_REMAINING,
        probe_every: int = RATE_PROBE_EVERY,
    ) -> None:
        self._gh = gh
        self.max_calls = max_calls
        self.min_remaining = min_remaining
        self._probe_every = probe_every
        self.calls = 0
        self.remaining: int | None = None

    def __call__(self, args: Sequence[str]) -> bytes:
        if self.calls >= self.max_calls:
            raise BudgetExhaustedError(f"reached --max-api-calls ({self.max_calls})")
        if self.calls % self._probe_every == 0:
            self._probe()
        self.calls += 1
        return self._gh(args)

    def _probe(self) -> None:
        try:
            body = json.loads(self._gh(["api", "rate_limit"]))
            remaining = int(body["resources"]["core"]["remaining"])
        except (CaptureError, ValueError, KeyError, TypeError):
            return  # cannot read it: the call limit still holds
        self.remaining = remaining
        if remaining <= self.min_remaining:
            raise BudgetExhaustedError(f"the rate limit is down to {remaining} calls")


def read_json(gh: Gh, endpoint: str) -> Any:
    """``GET`` an endpoint and parse it. Errors become ``CaptureError`` (never the body)."""
    try:
        return json.loads(gh(["api", endpoint]))
    except json.JSONDecodeError:
        raise CaptureError(f"unexpected answer for {endpoint.split('?')[0]}") from None


def is_gone(error: CaptureError) -> bool:
    """A 404 or 410 from ``gh``: the log expired, was never written or the job never started."""
    return bool(_HTTP_GONE.search(str(error)))


def clean_label(value: object, limit: int = 100) -> str:
    """Third-party text (a workflow, branch or job name) made safe to store: no hidden or control
    characters, one line, cut to ``limit``. Never trusted, never printed."""
    text = value if isinstance(value, str) else ""
    text = "".join(" " if has_hidden_controls(char) else char for char in text)
    return " ".join(text.split())[:limit]


def valid_repo(repo: str) -> bool:
    return bool(_REPO.match(repo)) and ".." not in repo


def valid_case_id(case_id: str) -> bool:
    return bool(_SAFE_ID.match(case_id))


def run_url(repo: str, run_id: int, attempt: int) -> str:
    return f"https://github.com/{repo}/actions/runs/{run_id}/attempts/{attempt}"


def repo_license(gh: Gh, repo: str) -> tuple[str | None, bool]:
    """The SPDX id GitHub reports for a repository, and whether it is private. One call."""
    data = read_json(gh, f"repos/{repo}")
    license_info = data.get("license") if isinstance(data, dict) else None
    spdx = license_info.get("spdx_id") if isinstance(license_info, dict) else None
    private = bool(data.get("private")) if isinstance(data, dict) else True
    return (spdx if isinstance(spdx, str) else None), private


def license_problem(spdx: str | None, private: bool, declared: str | None = None) -> str | None:
    """Why a repository cannot be a source, or None. Checked before any log is downloaded."""
    if private:
        return "repository is not public"
    if spdx is None or spdx == "NOASSERTION":
        return "no licence declared"
    if spdx not in ALLOWED_LICENSES:
        return "licence not allowed"
    if declared is not None and declared != spdx:
        return "licence differs from the manifest"
    return None
