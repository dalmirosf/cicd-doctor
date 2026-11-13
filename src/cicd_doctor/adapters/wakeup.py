"""Fan-out wake-up signal shared by the job queue adapters (``JobQueue.wait_for_jobs``)."""

import asyncio
from datetime import timedelta


class Wakeup:
    """Every coroutine waiting at the moment of ``notify_all`` is woken; later ones are not.

    A notification that arrives while nobody waits is dropped on purpose: consumers poll anyway,
    so the worst case is one poll interval of extra latency (D-47).
    """

    def __init__(self) -> None:
        self._waiters: set[asyncio.Event] = set()

    async def wait(self, max_wait: timedelta) -> bool:
        event = asyncio.Event()
        self._waiters.add(event)
        try:
            async with asyncio.timeout(max(max_wait.total_seconds(), 0.0)):
                await event.wait()
        except TimeoutError:
            return False
        finally:
            self._waiters.discard(event)
        return True

    def notify_all(self) -> None:
        for event in self._waiters:
            event.set()
