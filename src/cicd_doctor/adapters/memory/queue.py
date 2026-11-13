"""In-memory ``JobQueue``: reference behaviour for the contract tests and offline runs.

It follows the Postgres adapter step by step (SPEC §6, D-47, D-48), including the time taken
from the injected ``Clock``. It is single-process only: there is no locking because asyncio
code between two awaits never interleaves.
"""

import logging
from datetime import datetime, timedelta

from cicd_doctor.adapters.clock import SystemClock
from cicd_doctor.adapters.wakeup import Wakeup
from cicd_doctor.core.models import Delivery, Job, JobStatus, NewJob
from cicd_doctor.core.ports import Clock
from cicd_doctor.core.retry import truncate_error

logger = logging.getLogger(__name__)

_ACTIVE = (JobStatus.QUEUED, JobStatus.RUNNING)
EXHAUSTED_ERROR = "no attempts left: the last attempt did not finish before its lease expired"


class InMemoryJobQueue:
    """Implements ``JobQueue``."""

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock: Clock = clock or SystemClock()
        self._jobs: dict[int, Job] = {}
        self._dedupe_keys: set[str] = set()
        self.deliveries: dict[str, Delivery] = {}  # by delivery id; read by tests
        self._next_id = 1
        self._wakeup = Wakeup()

    # --- JobQueue ---------------------------------------------------------------------------

    async def enqueue(self, job: NewJob, delivery: Delivery | None) -> bool:
        if delivery is not None:
            key = str(delivery.delivery_id)
            if key in self.deliveries:
                return False
            self.deliveries[key] = delivery
        if job.dedupe_key is not None:
            if job.dedupe_key in self._dedupe_keys:
                return False
            self._dedupe_keys.add(job.dedupe_key)
        job_id = self._next_id
        self._next_id += 1
        self._jobs[job_id] = Job(
            id=job_id,
            kind=job.kind,
            payload=job.payload,
            dedupe_key=job.dedupe_key,
            status=JobStatus.QUEUED,
            attempts=0,
            max_attempts=job.max_attempts,
            run_after=job.run_after or self._clock.now(),
            locked_until=None,
            locked_by=None,
            last_error=None,
        )
        self._wakeup.notify_all()
        return True

    async def lease(self, worker_id: str, lease: timedelta) -> Job | None:
        now = self._clock.now()
        for job in list(self._jobs.values()):
            if job.status in _ACTIVE and job.attempts >= job.max_attempts and _lease_free(job, now):
                self._jobs[job.id] = job.model_copy(
                    update={
                        "status": JobStatus.DEAD,
                        "locked_until": None,
                        "last_error": EXHAUSTED_ERROR,
                    }
                )
                logger.error("job %s (%s) is dead: %s", job.id, job.kind, EXHAUSTED_ERROR)
        ready = [
            job
            for job in self._jobs.values()
            if job.status in _ACTIVE
            and job.run_after <= now
            and _lease_free(job, now)
            and job.attempts < job.max_attempts
        ]
        if not ready:
            return None
        chosen = min(ready, key=lambda job: (job.run_after, job.id))
        leased = chosen.model_copy(
            update={
                "status": JobStatus.RUNNING,
                "attempts": chosen.attempts + 1,
                "locked_by": worker_id,
                "locked_until": now + lease,
            }
        )
        self._jobs[leased.id] = leased
        return leased

    async def heartbeat(self, job: Job, lease: timedelta) -> bool:
        return self._update_leased(job, locked_until=self._clock.now() + lease)

    async def complete(self, job: Job) -> bool:
        return self._update_leased(job, status=JobStatus.DONE, locked_until=None)

    async def retry(self, job: Job, run_after: datetime, error: str) -> bool:
        return self._update_leased(
            job,
            status=JobStatus.QUEUED,
            run_after=run_after,
            last_error=truncate_error(error),
            locked_until=None,
            locked_by=None,
        )

    async def fail(self, job: Job, error: str, dead: bool) -> bool:
        return self._update_leased(
            job,
            status=JobStatus.DEAD if dead else JobStatus.FAILED,
            last_error=truncate_error(error),
            locked_until=None,
        )

    async def wait_for_jobs(self, max_wait: timedelta) -> bool:
        return await self._wakeup.wait(max_wait)

    # --- Inspection (tests, operations) -----------------------------------------------------

    async def get(self, job_id: int) -> Job | None:
        return self._jobs.get(job_id)

    def _update_leased(self, job: Job, **changes: object) -> bool:
        current = self._jobs.get(job.id)
        if (
            current is None
            or current.status is not JobStatus.RUNNING
            or current.attempts != job.attempts
        ):
            return False
        self._jobs[job.id] = current.model_copy(update=changes)
        return True


def _lease_free(job: Job, now: datetime) -> bool:
    return job.locked_until is None or job.locked_until < now
