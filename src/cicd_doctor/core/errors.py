"""Error taxonomy used to decide retry vs. give-up (SPEC §8, D-23)."""

from datetime import timedelta


class CicdDoctorError(Exception):
    """Base class for all errors raised on purpose by CICD Doctor."""


class TransientError(CicdDoctorError):
    """Worth retrying with backoff: GitHub 5xx, network, timeouts, rate limits, DB errors.

    ``retry_after`` is the wait the remote side asked for (``Retry-After``,
    ``x-ratelimit-reset``). When set, it replaces the computed backoff (SPEC §8).
    """

    def __init__(self, message: str = "", *, retry_after: timedelta | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PermanentError(CicdDoctorError):
    """Retrying cannot help: missing permission, expired logs, suspended installation."""


class LLMError(CicdDoctorError):
    """The LLM call failed in a way retrying cannot fix: a bad key, a rejected request, a refusal.

    It never fails a job: the diagnosis pipeline publishes the rules diagnosis with
    ``fallback_reason = "llm_error"`` (SPEC §8).
    """


class LLMTransientError(LLMError):
    """Timeout, 5xx, 429 or connection error: worth retrying in the same job (1 s, then 4 s)."""


class PolicyBlocked(CicdDoctorError):  # noqa: N818  (name fixed by SPEC §4.2)
    """An action was refused by policy (for example the LLM on a fork PR)."""
