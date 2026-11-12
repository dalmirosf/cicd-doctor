"""Error taxonomy used to decide retry vs. give-up (SPEC §8, D-23)."""


class CicdDoctorError(Exception):
    """Base class for all errors raised on purpose by CICD Doctor."""


class TransientError(CicdDoctorError):
    """Worth retrying with backoff: GitHub 5xx, network, timeouts, rate limits, DB errors."""


class PermanentError(CicdDoctorError):
    """Retrying cannot help: missing permission, expired logs, suspended installation."""


class PolicyBlocked(CicdDoctorError):  # noqa: N818  (name fixed by SPEC §4.2)
    """An action was refused by policy (for example the LLM on a fork PR)."""
