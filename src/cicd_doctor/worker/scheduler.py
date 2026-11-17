"""Daily scheduler: puts one ``purge`` job per UTC day in the queue (D-85).

Every worker process runs one. Each tick (15 minutes) it enqueues the job of today with
``dedupe_key = "purge:<date>"`` and ``run_after`` at ``PURGE_HOUR_UTC``; the ``UNIQUE (dedupe_key)``
of the queue makes exactly one of N schedulers create it, and the lease runs it once. There is no
elector and no extra lock. A queue that fails is logged and tried again on the next tick, like the
consumers of the worker do (D-49).
"""

import asyncio
import contextlib
import logging
from datetime import UTC, date, datetime, time, timedelta

from cicd_doctor.core.models import JobKind, NewJob
from cicd_doctor.core.ports import Clock, JobQueue

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL = timedelta(minutes=15)


def purge_dedupe_key(day: date) -> str:
    return f"purge:{day.isoformat()}"


class Scheduler:
    def __init__(
        self,
        queue: JobQueue,
        clock: Clock,
        *,
        hour_utc: int = 3,
        interval: timedelta = DEFAULT_INTERVAL,
    ) -> None:
        if not 0 <= hour_utc <= 23:
            raise ValueError(f"hour_utc must be 0..23, got {hour_utc}")
        self._queue = queue
        self._clock = clock
        self._hour = hour_utc
        self._interval = interval

    async def tick(self) -> bool:
        """Enqueue the purge of today. True when this call created the job; False when another
        scheduler (or an earlier tick) did, or when the queue failed."""
        now = self._clock.now().astimezone(UTC)
        run_after = datetime.combine(now.date(), time(self._hour), tzinfo=UTC)
        job = NewJob(
            kind=JobKind.PURGE,
            payload={},
            dedupe_key=purge_dedupe_key(now.date()),
            run_after=run_after,
        )
        try:
            created = await self._queue.enqueue(job, None)
        except Exception:
            logger.exception("scheduler: could not enqueue the daily purge, trying again later")
            return False
        if created:
            logger.info("scheduler: purge of %s scheduled at %s", now.date(), run_after.isoformat())
        return created

    async def run(self, stop: asyncio.Event) -> None:
        """Tick until ``stop`` is set."""
        while not stop.is_set():
            await self.tick()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._interval.total_seconds())
