"""What the HTTP API needs from outside, built by the composition root (D-55)."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

from fastapi import Request

from cicd_doctor.core.ports import Clock, JobQueue


@dataclass(frozen=True)
class ApiDependencies:
    queue: JobQueue
    # Current secret first, then the previous one while rotating. Empty: webhooks answer 503.
    webhook_secrets: tuple[bytes, ...]
    ready: Callable[[], Awaitable[bool]]  # readiness: the database answers
    clock: Clock
    max_attempts: int = 5  # for the jobs the webhook enqueues (JOB_MAX_ATTEMPTS)
    # Answer 503 well before GitHub gives up on the delivery after 10 s (NFR-01, D-56).
    enqueue_timeout: timedelta = timedelta(seconds=5)


def get_dependencies(request: Request) -> ApiDependencies:
    deps = getattr(request.app.state, "dependencies", None)
    if not isinstance(deps, ApiDependencies):
        raise RuntimeError("API dependencies are not initialised (lifespan not started?)")
    return deps
