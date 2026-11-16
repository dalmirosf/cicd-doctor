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


class LogsUnavailable(PermanentError):  # noqa: N818  (a state, like ``PolicyBlocked``)
    """GitHub no longer has the log of a job (404: not there, 410: expired or deleted).

    ``status_code`` keeps which one it was: the use case treats a 404 just after the job ended as
    transient (the log may not be ready yet) and a 410 as final (D-71).
    """

    def __init__(self, message: str = "", *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMError(CicdDoctorError):
    """The LLM call failed in a way retrying cannot fix: a bad key, a rejected request, a refusal.

    It never fails a job: the diagnosis pipeline publishes the rules diagnosis with
    ``fallback_reason = "llm_error"`` (SPEC §8).
    """


class LLMTransientError(LLMError):
    """Timeout, 5xx, 429 or connection error: worth retrying in the same job (1 s, then 4 s)."""


class PolicyBlocked(CicdDoctorError):  # noqa: N818  (name fixed by SPEC §4.2)
    """An action was refused by policy (for example the LLM on a fork PR)."""
