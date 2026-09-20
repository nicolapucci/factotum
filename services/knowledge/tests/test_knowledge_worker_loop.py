"""The loops that take jobs off the queue: real time, short waits."""

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from uuid import uuid4

import pytest

from factotum.contracts.identity import Owner, OwnerKind
from factotum.contracts.ids import TenantId
from factotum.contracts.knowledge import Collection, DocumentStatus
from factotum.knowledge.document import Limits, ParsedDocument
from factotum.knowledge.ingestion import IngestionSettings, Ingestor
from factotum.knowledge.loaders import sniff
from factotum.knowledge.loaders.reader import parse_document
from factotum.knowledge.store import InMemoryStore, Job, NewDocument
from factotum.knowledge.testing import FakeEmbedder, make_collection
from factotum.knowledge.worker import Worker


async def parse_here(data: bytes, media_type: str, limits: Limits) -> ParsedDocument:
    return parse_document(data, media_type, limits)


class World:
    def __init__(self, store: InMemoryStore, embedder: FakeEmbedder, tenant: TenantId) -> None:
        self.store = store
        self.embedder = embedder
        self.tenant = tenant
        self.collection: Collection

    @classmethod
    async def create(cls) -> "World":
        world = cls(InMemoryStore(), FakeEmbedder(), TenantId(uuid4()))
        owner = Owner(kind=OwnerKind.USER, id=uuid4())
        made = await make_collection(
            world.store, world.tenant, owner, dimensions=world.embedder.dimensions
        )
        moved = await world.store.begin_reindex(
            world.tenant, made.id, space_id="emb-fake", dimensions=world.embedder.dimensions
        )
        world.collection = await world.store.finish_reindex(world.tenant, moved.id)
        return world

    def worker(self, *, concurrency: int = 2) -> Worker:
        ingestor = Ingestor(
            self.store, self.embedder, settings=IngestionSettings(), parser=parse_here
        )
        return Worker(self.store, ingestor, poll_seconds=0.02, concurrency=concurrency)

    async def upload(self, text: str) -> None:
        data = text.encode()
        await self.store.add_document(
            self.tenant,
            NewDocument(
                collection_id=self.collection.id,
                title="notes",
                source="notes.txt",
                media_type=sniff.PLAIN,
                size_bytes=len(data),
                content_sha256=hashlib.sha256(data).hexdigest(),
                language=None,
                language_declared=False,
                data=data,
            ),
        )

    async def all_ready(self) -> bool:
        documents = await self.store.list_documents(self.tenant, self.collection.id)
        return bool(documents) and all(d.status is DocumentStatus.READY for d in documents)


@pytest.fixture
async def world() -> World:
    return await World.create()


async def until(condition: Callable[[], Awaitable[bool]], seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while not await condition():
        if time.monotonic() > deadline:
            raise AssertionError("it did not happen in time")
        await asyncio.sleep(0.01)


class TestRunOnce:
    async def test_with_nothing_to_do_it_says_so(self, world: World) -> None:
        assert await world.worker().run_once() is False

    async def test_it_does_the_next_job(self, world: World) -> None:
        await world.upload("Some words about routers and how they boot.")

        assert await world.worker().run_once() is True

        assert await world.all_ready()
        assert await world.worker().run_once() is False


class TestRunning:
    async def test_the_jobs_that_are_queued_get_done_and_the_worker_stops_when_told(
        self, world: World
    ) -> None:
        for n in range(4):
            await world.upload(f"Document number {n} about routers and their reset button.")
        stop = asyncio.Event()
        running = asyncio.create_task(world.worker().run(stop))

        await until(world.all_ready)
        stop.set()
        await asyncio.wait_for(running, 2)

    async def test_a_worker_with_nothing_to_do_stops_at_once_when_told(self, world: World) -> None:
        stop = asyncio.Event()
        slow = Worker(
            world.store,
            Ingestor(world.store, world.embedder, parser=parse_here),
            poll_seconds=30,
        )
        running = asyncio.create_task(slow.run(stop))
        await asyncio.sleep(0.05)

        stop.set()

        await asyncio.wait_for(running, 1)

    async def test_jobs_that_come_later_are_picked_up(self, world: World) -> None:
        stop = asyncio.Event()
        running = asyncio.create_task(world.worker().run(stop))
        await asyncio.sleep(0.05)

        await world.upload("A document that arrives while the worker is idle.")

        await until(world.all_ready)
        stop.set()
        await asyncio.wait_for(running, 2)

    async def test_several_jobs_are_done_at_the_same_time(self, world: World) -> None:
        active = 0
        most = 0

        async def busy() -> None:
            nonlocal active, most
            active += 1
            most = max(most, active)
            await asyncio.sleep(0.1)
            active -= 1

        world.embedder.during = busy
        for n in range(4):
            await world.upload(f"Document number {n} about routers and their reset button.")
        stop = asyncio.Event()
        running = asyncio.create_task(world.worker(concurrency=2).run(stop))

        await until(world.all_ready)
        stop.set()
        await asyncio.wait_for(running, 2)

        assert most == 2

    async def test_a_queue_that_fails_for_a_moment_is_waited_out(self, world: World) -> None:
        await world.upload("A document that waits for the queue to come back.")
        claim = world.store.claim_job
        failures = 2

        async def flaky(worker: str, *, lease_seconds: float) -> Job | None:
            nonlocal failures
            if failures:
                failures -= 1
                raise OSError("the database is away")
            return await claim(worker, lease_seconds=lease_seconds)

        world.store.claim_job = flaky  # type: ignore[method-assign]
        stop = asyncio.Event()
        running = asyncio.create_task(world.worker(concurrency=1).run(stop))

        await until(world.all_ready)
        stop.set()
        await asyncio.wait_for(running, 2)

        assert failures == 0
