"""Use case: the daily purge (FR-18, D-34, D-85).

Every cut is computed from the injected ``Clock``, so a ``FakeClock`` moves the purge. Every step
deletes by a time predicate, so running it again right after finds nothing left to do. The log
carries counts only, never content (D-34).
"""

import logging
from dataclasses import dataclass
from datetime import timedelta

from cicd_doctor.core.models import PurgeCounts
from cicd_doctor.core.ports import Clock, CommandLog, FailureStore, InstallationStore, JobQueue

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PurgeConfig:
    """Retention windows, in days (SPEC §2 FR-18, D-85)."""

    deliveries_days: int = 30
    failures_days: int = 90  # run attempts, with their failures and diagnoses and the publications
    jobs_days: int = 30  # finished jobs: their payloads carry repository data
    stale_after_days: int = 30  # an open failure turns ``stale``
    uninstall_grace_days: int = 7  # after an uninstall or a removed repository
    commands_days: int = 30  # accepted explain commands (D-93)


async def run_purge(
    queue: JobQueue,
    store: FailureStore,
    installations: InstallationStore,
    clock: Clock,
    config: PurgeConfig,
    commands: CommandLog | None = None,
) -> PurgeCounts:
    """Apply the retention rules and return what was removed, by table."""
    now = clock.now()
    stale = await store.mark_stale(now - timedelta(days=config.stale_after_days), now)
    counts = await queue.purge(
        now - timedelta(days=config.deliveries_days), now - timedelta(days=config.jobs_days)
    )
    failures_cut = now - timedelta(days=config.failures_days)
    counts += await store.purge(failures_cut, failures_cut)
    gone = await installations.gone_repositories(now - timedelta(days=config.uninstall_grace_days))
    counts += await store.purge_repositories(gone)
    if commands is not None:
        counts += await commands.purge(now - timedelta(days=config.commands_days))
    logger.info(
        "purge done: %d failures marked stale, %d rows deleted (%s), %d repositories gone",
        stale,
        counts.total,
        ", ".join(f"{table}={n}" for table, n in sorted(counts.deleted.items())),
        len(gone),
    )
    return counts
