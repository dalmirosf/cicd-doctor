"""Logging configuration: one place for every record the process writes (D-117, NFR-10).

``structlog`` renders each record, and the stdlib ``logging`` module stays the entry point, so the
modules of ``core/`` (which may not import structlog) and the third-party libraries (uvicorn,
huggingface_hub, httpx2) end up in the same stream. ``LOG_FORMAT=json`` writes one JSON object
per line on stderr (what CloudWatch Logs wants); ``text`` is the readable form for a terminal.

Every record goes through ``redact_processor`` (the third redaction pass of SPEC §9.2, D-59): each
string value, the message and the text of an exception are redacted, and a value stored under a
key that names a secret is replaced whatever it looks like. Correlation fields (``delivery_id``,
``job_id``, ``run_id``, ``installation_id``, ``repository``) come from ``structlog.contextvars``,
bound by the webhook handler and the worker for the duration of one request or job.
"""

import logging
import sys
import traceback
from collections.abc import Mapping, MutableMapping, Sequence
from typing import Any, Literal

import structlog
from structlog.typing import EventDict, Processor

from cicd_doctor.adapters.structured_log import enrichers
from cicd_doctor.core.redaction import placeholder, redact

LogFormat = Literal["json", "text"]

# At INFO these print the URL of every request, and the URL of a log blob carries its signature
# in the query (D-67). Their children (``httpcore2.http11`` and so on) inherit the level.
# ``httpx``, ``httpcore`` and ``huggingface_hub`` are the ones of the embedding model download
# (fastembed): public URLs today, but the worker should not print any request URL (D-87).
HTTP_LOGGERS = ("httpx2", "httpcore2", "httpx", "httpcore", "huggingface_hub")

# A key that names a secret hides its value even when the value looks harmless.
SECRET_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "authorization",
    "private_key",
    "api_key",
)

# An exception message can echo what the code was parsing (a pydantic error repeats the input), so
# the rendered traceback is cut: the frames and the type matter, a long message does not.
MAX_EXCEPTION_CHARS = 6000
MAX_STRING_CHARS = 8000
_MAX_DEPTH = 6
_TRUNCATED = "…[truncated]"

_OWN_HANDLER = "_cicd_doctor_handler"
CORRELATION_FIELDS = ("delivery_id", "job_id", "run_id", "installation_id", "repository")


def _is_secret_key(key: object) -> bool:
    lowered = str(key).lower().replace("-", "_")
    return any(part in lowered for part in SECRET_KEY_PARTS)


def _clean(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        text = redact(value)
        return text if len(text) <= MAX_STRING_CHARS else text[:MAX_STRING_CHARS] + _TRUNCATED
    if depth >= _MAX_DEPTH:
        return redact(str(value))[:MAX_STRING_CHARS]
    if isinstance(value, Mapping):
        return {
            key: placeholder("secret_key") if _is_secret_key(key) else _clean(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, Sequence | set | frozenset) and not isinstance(value, bytes | bytearray):
        return [_clean(item, depth + 1) for item in value]
    if isinstance(value, bytes | bytearray):
        return f"<{len(value)} bytes>"
    return value


def format_exception(_: Any, __: str, event_dict: EventDict) -> EventDict:
    """Turn ``exc_info`` into a redacted, bounded ``exception`` string."""
    exc_info = event_dict.pop("exc_info", None)
    if not exc_info:
        return event_dict
    if exc_info is True:
        exc_info = sys.exc_info()
    if isinstance(exc_info, BaseException):
        exc_info = (type(exc_info), exc_info, exc_info.__traceback__)
    if isinstance(exc_info, tuple) and exc_info[0] is not None:
        rendered = "".join(traceback.format_exception(*exc_info))
        text = redact(rendered)
        if len(text) > MAX_EXCEPTION_CHARS:
            text = text[:MAX_EXCEPTION_CHARS] + _TRUNCATED
        event_dict["exception"] = text
    return event_dict


def redact_processor(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> EventDict:
    """The third redaction pass: every value of the record (D-117)."""
    return {
        key: placeholder("secret_key") if _is_secret_key(key) else _clean(value)
        for key, value in event_dict.items()
    }


def build_formatter(log_format: LogFormat) -> structlog.stdlib.ProcessorFormatter:
    """The formatter of the one handler. Redaction runs here, after every other processor, so it
    sees both the records of structlog loggers and the stdlib ones (foreign)."""
    renderer: list[Processor] = (
        [structlog.processors.EventRenamer("message"), structlog.processors.JSONRenderer()]
        if log_format == "json"
        else [structlog.dev.ConsoleRenderer(colors=False)]
    )
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[*enrichers(), structlog.stdlib.ExtraAdder()],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            format_exception,
            redact_processor,
            *renderer,
        ],
    )


def configure_logging(log_format: LogFormat = "text", level: str = "INFO") -> None:
    """Send every stdlib and structlog record to stderr in ``log_format``. Idempotent."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(build_formatter(log_format))
    setattr(handler, _OWN_HANDLER, True)
    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, _OWN_HANDLER, False):  # leave pytest's and anyone else's alone
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())
    for name in HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    # uvicorn installs its own plain-text handlers: drop them so its records reach ours.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    structlog.configure(
        processors=[*enrichers(), structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
