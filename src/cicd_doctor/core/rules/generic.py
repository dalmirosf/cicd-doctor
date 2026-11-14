"""Generic GitHub Actions rules (SPEC §10)."""

import re

from cicd_doctor.core.models import Category
from cicd_doctor.core.rules.engine import Rule, compile_all

GENERIC_RULES: tuple[Rule, ...] = (
    Rule(
        id="GHA-TIMEOUT",
        ecosystem="generic",
        patterns=compile_all(r"has exceeded the maximum execution time of"),
        on_conclusion="timed_out",
        category=Category.TIMEOUT,
        confidence=0.95,
        priority=3,
        cause="The job ran longer than its `timeout-minutes` limit and GitHub cancelled it.",
        fix=(
            "Find what made the job slow (a hang, a retry loop, a slow test) or raise the job's "
            "`timeout-minutes` if the work legitimately takes longer."
        ),
    ),
    Rule(
        id="GHA-CANCEL-TIMEOUT",
        ecosystem="generic",
        patterns=compile_all(r"has timed out after \d+ minutes?", r"The operation was canceled\."),
        category=Category.TIMEOUT,
        confidence=0.70,
        priority=1,
        cause="A step was cancelled, most likely by its `timeout-minutes` limit.",
        fix=(
            "Check which step hit its `timeout-minutes` limit; make it faster or raise the limit "
            "if the work legitimately takes longer."
        ),
    ),
    Rule(
        id="GHA-OOM",
        ecosystem="generic",
        patterns=compile_all(
            r"exit code 137\b", r"\bMemoryError\b", r"JavaScript heap out of memory"
        ),
        tail_patterns=compile_all(r"\bKilled\b"),
        category=Category.INFRA_OR_DEPLOY,
        confidence=0.80,
        priority=2,
        cause="The process ran out of memory and was killed (exit code 137 / OOM).",
        fix=(
            "Reduce the memory the step needs (fewer parallel workers, smaller batches, "
            "`NODE_OPTIONS=--max-old-space-size`) or run it on a larger runner."
        ),
    ),
    Rule(
        id="GHA-RUNNER-LOST",
        ecosystem="generic",
        patterns=compile_all(
            r"The runner has received a shutdown signal",
            r"lost communication with the server",
            r"No space left on device",
        ),
        category=Category.INFRA_OR_DEPLOY,
        confidence=0.85,
        priority=3,
        cause="The runner failed: it was shut down, lost its connection or ran out of disk.",
        fix=(
            "Re-run the job. If the disk filled up, free space before the heavy step (remove "
            "unused toolchains, prune Docker images) or write less to the runner's disk."
        ),
    ),
    Rule(
        id="GHA-INPUT-REQUIRED",
        ecosystem="generic",
        patterns=compile_all(r"Input required and not supplied: (?P<input>\S+)"),
        category=Category.CONFIG_OR_SECRETS,
        confidence=0.90,
        priority=3,
        cause="The action input `{input}` is required but was not supplied.",
        fix=(
            "Add `{input}` under the step's `with:`; if it comes from a secret or variable, "
            "check that its name is spelled right and that it is defined for this repository."
        ),
    ),
    Rule(
        id="GHA-SECRET-EMPTY",
        ecosystem="generic",
        patterns=(
            re.compile(r"Bad credentials"),
            re.compile(r"Error: .*\btoken\b.*\b(empty|not set)\b", re.IGNORECASE),
            re.compile(r"Credentials could not be loaded"),
        ),
        category=Category.CONFIG_OR_SECRETS,
        confidence=0.80,
        priority=2,
        cause="A credential used by the step is empty or invalid (a missing or misspelled secret).",
        fix=(
            "Check the secret names used by the workflow against the repository's secrets; "
            "secrets are empty for fork pull requests and when a name is misspelled."
        ),
    ),
    Rule(
        id="GHA-DNS",
        ecosystem="generic",
        patterns=compile_all(
            r"Could not resolve host",
            r"getaddrinfo (ENOTFOUND|EAI_AGAIN)",
            r"Temporary failure in name resolution",
            r"Name or service not known",
            r"nodename nor servname provided",
        ),
        category=Category.NETWORK,
        confidence=0.85,
        priority=2,
        cause="A host name could not be resolved (DNS).",
        fix=(
            "Check the host name for typos; if it is right, the DNS failure may be transient, "
            "so re-run the job or add a retry."
        ),
    ),
    Rule(
        id="GHA-CONN",
        ecosystem="generic",
        patterns=compile_all(
            r"Connection reset by peer",
            r"\bECONNRESET\b",
            r"\bECONNREFUSED\b",
            r"\bETIMEDOUT\b",
            r"TLS handshake timeout",
            r"Connection refused",
        ),
        category=Category.NETWORK,
        confidence=0.75,
        priority=1,
        cause="A network connection was refused, reset or timed out.",
        fix=(
            "Check that the service the step connects to is running and reachable from the "
            "runner (host, port, service container health); add a retry for transient failures."
        ),
    ),
    Rule(
        id="GHA-RATE-LIMIT",
        ecosystem="generic",
        patterns=compile_all(
            r"API rate limit exceeded", r"429 Too Many Requests", r"secondary rate limit"
        ),
        category=Category.NETWORK,
        confidence=0.85,
        priority=2,
        cause="An API rate limit was hit (HTTP 429).",
        fix=(
            "Authenticate the requests (for the GitHub API, pass `GITHUB_TOKEN`), cache what is "
            "downloaded, and back off before retrying."
        ),
    ),
)
