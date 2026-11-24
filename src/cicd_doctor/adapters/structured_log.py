"""The structlog logger of ``api/`` and ``worker/`` (D-117).

``get_logger`` wraps the stdlib logger of the same name explicitly, instead of relying on a global
``structlog.configure``: an unconfigured structlog prints to **stdout**, which would corrupt the
output of any process that imports a module before ``app.logging_config.configure_logging`` ran
(the crash-test worker reads its protocol from stdout). Whatever the handlers of the stdlib logger
are, the record is formatted by them; ``configure_logging`` installs the JSON or text one.
"""

import logging

import structlog
from structlog.typing import Processor


def enrichers() -> list[Processor]:
    """Fields added to every record, ours and foreign: the correlation context, level, logger
    name and an UTC timestamp."""
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.wrap_logger(
        logging.getLogger(name),
        processors=[*enrichers(), structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.stdlib.BoundLogger,
    )
    return logger
