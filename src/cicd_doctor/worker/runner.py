"""Job consumer: N asyncio consumers that lease jobs and run the handler of their kind.

Error handling follows SPEC §8 and D-23, with the details of D-49:

- ``TransientError`` and unexpected exceptions: retry with full-jitter backoff (or the wait the
  error asks for), and ``dead`` once ``max_attempts`` is used up.
- ``PermanentError`` and kinds without a handler: ``failed``.
- While a handler runs, the lease is extended every ``lease / 3``. If the queue says the lease is
  gone, the handler is cancelled: another worker owns the job now.
- Cancellation (shutdown) leaves the job ``running``; its lease expires and it is leased again.
"""

import asyncio
import logging
import os
import random
import socket
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from contextlib import suppress
from datetime import timedelta
from typing import Any

from cicd_doctor.core.errors import PermanentError, TransientError
from cicd_doctor.core.models import Job, JobKind
from cicd_doctor.core.ports import Clock, JobQueue
from cicd_doctor.core.redaction import redact
from cicd_doctor.core.retry import backoff_delay

logger = logging.getLogger(__name__)

Handler = Callable[[Job], Coroutine[Any, Any, None]]

DEFAULT_LEASE = timedelta(seconds=300)
DEFAULT_POLL_INTERVAL = timedelta(seconds=5)
DEFAULT_SHUTDOWN_GRACE = timedelta(seconds=30)


class LeaseLostError(Exception):
    """The queue no longer recognises our lease on the job."""


class Worker:
    def __init__(
        self,
        queue: JobQueue,
        handlers: Mapping[JobKind, Handler],
        clock: Clock,
        *,
        concurrency: int = 4,
        lease: timedelta = DEFAULT_LEASE,
        poll_interval: timedelta = DEFAULT_POLL_INTERVAL,
        shutdown_grace: timedelta = DEFAULT_SHUTDOWN_GRACE,
        name: str | None = None,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        if concurrency < 1:
            raise ValueError(f"concurrency must be >= 1, got {concurrency}")
        self._queue = queue
        self._handlers = dict(handlers)
        self._clock = clock
        self._concurrency = concurrency
        self._lease = lease
        self._poll_interval = poll_interval
        self._shutdown_grace = shutdown_grace
        self._jitter = jitter
        self.name = name or f"{socket.gethostname()}:{os.getpid()}"

    async def run(self, stop: asyncio.Event) -> None:
        """Consume until ``stop`` is set (or this coroutine is cancelled).

        On the way out, running jobs get ``shutdown_grace`` to finish; then they are cancelled.
        """
        consumers = [
            asyncio.create_task(
                self._consume(f"{self.name}:{index}", stop), name=f"consumer-{index}"
            )
            for index in range(self._concurrency)
        ]
        logger.info("worker %s started with %d consumers", self.name, self._concurrency)
        try:
            await stop.wait()
        finally:
            stop.set()
            _, pending = await asyncio.wait(consumers, timeout=self._shutdown_grace.total_seconds())
            for task in pending:
                task.cancel()
            await asyncio.gather(*consumers, return_exceptions=True)
            logger.info("worker %s stopped", self.name)

    async def run_one(self, consumer_id: str) -> bool:
        """Lease one job and run it to an outcome. Return False when nothing was ready."""
        job = await self._queue.lease(consumer_id, self._lease)
        if job is None:
            return False
        await self._execute(job)
        return True

    # --- Internals --------------------------------------------------------------------------

    async def _consume(self, consumer_id: str, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                processed = await self.run_one(consumer_id)
            except Exception:
                # The queue itself failed (for example the database is down): keep the consumer
                # alive and try again after a poll interval.
                logger.exception("consumer %s could not use the queue", consumer_id)
                processed = False
            if not processed:
                await self._idle(stop)

    async def _idle(self, stop: asyncio.Event) -> None:
        """Sleep until new jobs are announced, ``stop`` is set, or a poll interval passes."""
        wake = asyncio.create_task(self._queue.wait_for_jobs(self._poll_interval))
        stopped = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait({wake, stopped}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (wake, stopped):
                task.cancel()
            await asyncio.gather(wake, stopped, return_exceptions=True)

    async def _execute(self, job: Job) -> None:
        handler = self._handlers.get(job.kind)
        if handler is None:
            await self._queue.fail(job, f"no handler registered for kind {job.kind}", dead=False)
            logger.error("job %s (%s) failed: no handler registered", job.id, job.kind)
            return
        logger.info("job %s (%s) started, attempt %d", job.id, job.kind, job.attempts)
        try:
            await self._run_with_heartbeat(job, handler)
        except LeaseLostError:
            logger.warning("job %s (%s): lease lost, another worker owns it", job.id, job.kind)
        except PermanentError as exc:
            await self._record(self._queue.fail(job, _describe(exc), dead=False), job)
            logger.error("job %s (%s) failed: %s", job.id, job.kind, _describe(exc))
        except TransientError as exc:
            await self._retry_or_bury(job, exc, exc.retry_after)
        except Exception as exc:
            # Unknown errors are retried like transient ones, so no work is lost; a real bug ends
            # up dead after max_attempts, logged at ERROR (D-49).
            logger.exception("job %s (%s) raised an unexpected error", job.id, job.kind)
            await self._retry_or_bury(job, exc, None)
        else:
            await self._record(self._queue.complete(job), job)
            logger.info("job %s (%s) done", job.id, job.kind)

    async def _run_with_heartbeat(self, job: Job, handler: Handler) -> None:
        task = asyncio.create_task(handler(job), name=f"job-{job.id}")
        interval = self._lease.total_seconds() / 3
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=interval)
                if done:
                    task.result()  # re-raises the handler's exception
                    return
                try:
                    still_ours = await self._queue.heartbeat(job, self._lease)
                except Exception:
                    logger.warning(
                        "job %s: heartbeat failed, will try again", job.id, exc_info=True
                    )
                    continue
                if not still_ours:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                    raise LeaseLostError(job.id)
        except asyncio.CancelledError:
            task.cancel()
            raise

    async def _retry_or_bury(
        self, job: Job, exc: BaseException, retry_after: timedelta | None
    ) -> None:
        error = _describe(exc)
        if job.attempts >= job.max_attempts:
            await self._record(self._queue.fail(job, error, dead=True), job)
            logger.error(
                "job %s (%s) is dead after %d attempts: %s", job.id, job.kind, job.attempts, error
            )
            return
        delay = (
            retry_after if retry_after is not None else backoff_delay(job.attempts, self._jitter())
        )
        run_after = self._clock.now() + delay
        await self._record(self._queue.retry(job, run_after, error), job)
        logger.warning(
            "job %s (%s) attempt %d failed, retrying at %s: %s",
            job.id,
            job.kind,
            job.attempts,
            run_after.isoformat(),
            error,
        )

    @staticmethod
    async def _record(outcome: Awaitable[bool], job: Job) -> None:
        if not await outcome:
            logger.warning("job %s: outcome not recorded, the lease was no longer ours", job.id)


def _describe(exc: BaseException) -> str:
    """``"<ExceptionType>: <message>"``, redacted (D-49, D-59); the queue truncates it to 2 KB."""
    return redact(f"{type(exc).__name__}: {exc}")
