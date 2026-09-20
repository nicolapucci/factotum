"""The workers that take jobs off the queue and do them.

A few loops run side by side in one process, each claiming the next job that is due. Nothing is
lost when a worker stops: a job it was doing keeps its lease until it runs out, and is then given to
another. A database that is briefly not there is waited out, not fatal.
"""

import asyncio
import contextlib
import logging

from factotum.knowledge.ingestion import Ingestor
from factotum.knowledge.store import KnowledgeStore

logger = logging.getLogger("factotum.knowledge")


class Worker:
    def __init__(
        self,
        store: KnowledgeStore,
        ingestor: Ingestor,
        *,
        name: str = "knowledge-worker",
        lease_seconds: float = 600.0,
        poll_seconds: float = 2.0,
        concurrency: int = 2,
    ) -> None:
        self._store = store
        self._ingestor = ingestor
        self._name = name
        self._lease_seconds = lease_seconds
        self._poll_seconds = poll_seconds
        self._concurrency = max(1, concurrency)

    async def run_once(self, name: str | None = None) -> bool:
        """Claim the next job that is due and do it. False when there was none."""
        job = await self._store.claim_job(name or self._name, lease_seconds=self._lease_seconds)
        if job is None:
            return False
        await self._ingestor.run(job)
        return True

    async def run(self, stop: asyncio.Event) -> None:
        """Work until ``stop`` is set. What a worker is in the middle of is finished first."""
        async with asyncio.TaskGroup() as group:
            for index in range(self._concurrency):
                group.create_task(self._loop(f"{self._name}-{index}", stop))

    async def _loop(self, name: str, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                worked = await self.run_once(name)
            except Exception:
                # The queue itself failed (the database is away): wait and ask again.
                logger.exception("a worker could not reach the job queue", extra={"worker": name})
                worked = False
            if not worked:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), self._poll_seconds)
