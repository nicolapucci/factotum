"""Reading documents into a collection, against the store in memory and an embedder that needs no
model. The clock is the test's: the waits between attempts are seconds that pass when it says."""

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from factotum.contracts.errors import (
    CollectionNotFoundError,
    EmbeddingNotConfiguredError,
    GatewayUnavailableError,
    RateLimitedError,
)
from factotum.contracts.identity import Owner, OwnerKind
from factotum.contracts.ids import CollectionId, TenantId
from factotum.contracts.knowledge import (
    Collection,
    CollectionStatus,
    Document,
    DocumentStatus,
)
from factotum.knowledge.chunker import ChunkingConfig
from factotum.knowledge.document import Block, Limits, ParsedDocument
from factotum.knowledge.ingestion import IngestionSettings, Ingestor
from factotum.knowledge.loaders import sniff
from factotum.knowledge.loaders.reader import parse_document
from factotum.knowledge.store import InMemoryStore, Job, JobKind, NewDocument
from factotum.knowledge.testing import FakeEmbedder, make_collection

PROSE = "\n\n".join(
    f"Paragraph {n} explains how the router boots and how to reset it when it does not." * 2
    for n in range(12)
)
"""Twelve paragraphs of about 160 characters each."""

SMALL = ChunkingConfig(child_tokens=64, parent_tokens=200)
"""A piece of about 190 characters: each paragraph of the prose is one."""

MARKDOWN = (
    "# Networking\n\n## Routers\n\nHold the reset button for ten seconds.\n\n"
    "## Switches\n\nSwitches have no reset button at all."
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def parse_here(data: bytes, media_type: str, limits: Limits) -> ParsedDocument:
    return parse_document(data, media_type, limits)


@dataclass
class Rig:
    store: InMemoryStore
    embedder: FakeEmbedder
    clock: Clock
    tenant: TenantId
    owner: Owner
    collection: Collection
    settings: IngestionSettings
    parses: list[str] = field(default_factory=list)
    parser: Callable[[bytes, str, Limits], Awaitable[ParsedDocument]] = parse_here

    def __post_init__(self) -> None:
        self.ingestor = self.build()

    def build(self) -> Ingestor:
        async def parse(data: bytes, media_type: str, limits: Limits) -> ParsedDocument:
            self.parses.append(media_type)
            return await self.parser(data, media_type, limits)

        return Ingestor(self.store, self.embedder, settings=self.settings, parser=parse)

    def configure(self, **changes: object) -> None:
        self.settings = replace(self.settings, **changes)  # type: ignore[arg-type]
        self.ingestor = self.build()

    async def upload(
        self,
        content: str | bytes,
        *,
        name: str = "notes.txt",
        media_type: str = sniff.PLAIN,
        language: str | None = None,
        declared: bool = False,
    ) -> Document:
        data = content.encode() if isinstance(content, str) else content
        return await self.store.add_document(
            self.tenant,
            NewDocument(
                collection_id=self.collection.id,
                title=name,
                source=name,
                media_type=media_type,
                size_bytes=len(data),
                content_sha256=hashlib.sha256(data).hexdigest(),
                language=language,
                language_declared=declared,
                data=data,
            ),
        )

    async def work(self) -> Job | None:
        """Claim the next job and do it."""
        job = await self.store.claim_job("test-worker", lease_seconds=60)
        if job is not None:
            await self.ingestor.run(job)
        return job

    async def document(self, document: Document) -> Document:
        return await self.store.get_document(self.tenant, document.id)

    async def the_collection(self) -> Collection:
        return await self.store.get_collection(self.tenant, self.collection.id)

    def embedded_texts(self) -> list[str]:
        return [text for _, texts in self.embedder.calls for text in texts]


@pytest.fixture
async def rig() -> Rig:
    clock = Clock()
    store = InMemoryStore(clock=clock)
    embedder = FakeEmbedder()
    tenant = TenantId(uuid4())
    owner = Owner(kind=OwnerKind.USER, id=uuid4())
    collection = await make_collection(store, tenant, owner, dimensions=embedder.dimensions)
    collection = await store.begin_reindex(
        tenant, collection.id, space_id=embedder.space_id, dimensions=embedder.dimensions
    )
    collection = await store.finish_reindex(tenant, collection.id)
    settings = IngestionSettings(retry_base_seconds=30, max_attempts=5, batch=32)
    return Rig(store, embedder, clock, tenant, owner, collection, settings)


class TestIngesting:
    async def test_a_document_becomes_pieces_that_can_be_found(self, rig: Rig) -> None:
        document = await rig.upload(PROSE)

        await rig.work()

        ready = await rig.document(document)
        assert ready.status is DocumentStatus.READY
        assert ready.chunk_count > 0
        assert ready.chunks_embedded == ready.chunk_count
        assert ready.error_code is None
        hits = await rig.store.search_text(rig.tenant, [rig.collection.id], "router boots", limit=3)
        assert hits

    async def test_the_job_is_done_when_the_document_is(self, rig: Rig) -> None:
        await rig.upload(PROSE)

        await rig.work()

        assert await rig.store.claim_job("worker", lease_seconds=60) is None

    async def test_the_language_is_detected_and_kept_apart_from_one_that_was_said(
        self, rig: Rig
    ) -> None:
        english = await rig.upload(PROSE, name="english.txt")
        italian = await rig.upload(
            "\n\n".join(
                "Il router conserva la propria configurazione in un file e la rilegge di nuovo "
                "ogni volta che il dispositivo si avvia, anche dopo un'interruzione."
                for _ in range(6)
            ),
            name="italian.txt",
        )

        await rig.work()
        await rig.work()

        assert (await rig.document(english)).language == "en"
        detected = await rig.document(italian)
        assert (detected.language, detected.language_declared) == ("it", False)

    async def test_a_language_the_uploader_said_is_kept(self, rig: Rig) -> None:
        document = await rig.upload(PROSE, language="de", declared=True)

        await rig.work()

        kept = await rig.document(document)
        assert (kept.language, kept.language_declared) == ("de", True)

    async def test_the_pieces_are_embedded_with_their_place_in_front(self, rig: Rig) -> None:
        await rig.upload(MARKDOWN, name="guide.md", media_type=sniff.MARKDOWN)

        await rig.work()

        assert "Networking > Routers\n\nHold the reset button for ten seconds." in (
            rig.embedded_texts()
        )
        assert "Networking > Switches\n\nSwitches have no reset button at all." in (
            rig.embedded_texts()
        )

    async def test_it_embeds_as_the_owner_of_the_collection(self, rig: Rig) -> None:
        await rig.upload(PROSE)

        await rig.work()

        assert {owner for owner, _ in rig.embedder.calls} == {rig.owner}

    async def test_pieces_are_embedded_a_batch_at_a_time(self, rig: Rig) -> None:
        rig.configure(chunking=SMALL, batch=5)
        document = await rig.upload(PROSE)

        await rig.work()

        ready = await rig.document(document)
        sizes = [len(texts) for _, texts in rig.embedder.calls]
        assert ready.chunk_count >= 10
        assert max(sizes) == 5
        assert sum(sizes) == ready.chunk_count

    async def test_the_job_is_kept_alive_while_it_works(
        self, rig: Rig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rig.configure(chunking=SMALL, batch=5)
        spy = AsyncMock(wraps=rig.store.extend_job)
        monkeypatch.setattr(rig.store, "extend_job", spy)
        await rig.upload(PROSE)

        await rig.work()

        assert spy.await_count == len(rig.embedder.calls)
        assert spy.await_args is not None
        assert spy.await_args.kwargs == {"lease_seconds": rig.settings.lease_seconds}

    async def test_an_index_that_cannot_be_built_does_not_fail_the_document(
        self, rig: Rig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def broken(tenant: TenantId, collection_id: CollectionId) -> None:
            raise RuntimeError("the disk is full")

        monkeypatch.setattr(rig.store, "optimize", broken)
        document = await rig.upload(PROSE)

        await rig.work()

        ready = await rig.document(document)
        assert ready.status is DocumentStatus.READY
        assert ready.chunks_embedded == ready.chunk_count > 0
        assert await rig.store.claim_job("worker", lease_seconds=60) is None

    async def test_a_collection_deleted_while_its_index_is_built_is_dropped_without_a_word(
        self, rig: Rig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def gone(tenant: TenantId, collection_id: CollectionId) -> None:
            raise CollectionNotFoundError("the collection does not exist")

        monkeypatch.setattr(rig.store, "optimize", gone)
        await rig.upload(PROSE)

        job = await rig.work()

        assert job is not None
        assert await rig.store.claim_job("worker", lease_seconds=60) is None

    async def test_a_real_pdf_is_read_by_the_isolated_reader(
        self, rig: Rig, pdf: Callable[..., bytes]
    ) -> None:
        book = pdf(
            {
                "Chapter 1": ["Hold the reset button of the router for ten seconds. " * 4],
                "Chapter 2": ["The warranty covers manufacturing defects for two years. " * 4],
            }
        )
        rig.ingestor = Ingestor(rig.store, rig.embedder, settings=rig.settings)
        document = await rig.upload(book, name="book.pdf", media_type=sniff.PDF)

        await rig.work()

        ready = await rig.document(document)
        assert ready.status is DocumentStatus.READY
        [hit] = await rig.store.search_text(rig.tenant, [rig.collection.id], "warranty", limit=1)
        assert (hit.page, hit.headings) == (2, ("Chapter 2",))


class TestADocumentThatCannotBeRead:
    async def test_it_fails_with_a_reason_and_is_not_tried_again(self, rig: Rig) -> None:
        document = await rig.upload(b"%PDF-1.4 this is not a pdf", media_type=sniff.PDF)

        await rig.work()

        failed = await rig.document(document)
        assert failed.status is DocumentStatus.FAILED
        assert failed.error_code == "unsupported_document"
        assert failed.error_message == "the PDF could not be read"
        assert failed.chunk_count == 0
        assert rig.embedder.calls == []
        rig.clock.advance(10_000)
        assert await rig.store.claim_job("worker", lease_seconds=60) is None

    async def test_a_document_with_nothing_but_white_space_fails(self, rig: Rig) -> None:
        document = await rig.upload("  \n\n \t \n")

        await rig.work()

        failed = await rig.document(document)
        assert failed.status is DocumentStatus.FAILED
        assert failed.error_message == "there is no text in the document"

    async def test_a_scanned_pdf_says_that_it_needs_ocr(
        self, rig: Rig, blank_pdf: Callable[..., bytes]
    ) -> None:
        document = await rig.upload(blank_pdf(), name="scan.pdf", media_type=sniff.PDF)

        await rig.work()

        failed = await rig.document(document)
        assert failed.status is DocumentStatus.FAILED
        assert failed.error_message is not None
        assert "OCR" in failed.error_message

    async def test_a_document_over_the_limit_fails(self, rig: Rig) -> None:
        rig.configure(limits=Limits(max_bytes=10))
        document = await rig.upload("this text is longer than ten bytes")

        await rig.work()

        failed = await rig.document(document)
        assert (failed.status, failed.error_code) == (DocumentStatus.FAILED, "document_too_large")

    async def test_the_pieces_of_a_document_that_failed_are_not_searched(self, rig: Rig) -> None:
        document = await rig.upload(PROSE)
        rig.embedder.failures = [EmbeddingNotConfiguredError("no binding")]

        await rig.work()

        assert (await rig.document(document)).chunk_count == 0
        assert await rig.store.search_text(rig.tenant, [rig.collection.id], "router", limit=5) == []


class TestTheEmbeddingModel:
    async def test_an_owner_without_an_embedding_model_is_told_how_to_go_on(self, rig: Rig) -> None:
        document = await rig.upload(PROSE)
        rig.embedder.failures = [EmbeddingNotConfiguredError("no binding")]

        await rig.work()

        failed = await rig.document(document)
        assert (failed.status, failed.error_code) == (
            DocumentStatus.FAILED,
            "embedding_not_configured",
        )
        assert failed.error_message is not None
        assert "set one up" in failed.error_message
        assert await rig.store.claim_job("worker", lease_seconds=60) is None

    async def test_a_gateway_that_is_down_is_tried_again_later(self, rig: Rig) -> None:
        document = await rig.upload(PROSE)
        rig.embedder.failures = [GatewayUnavailableError("down")]

        await rig.work()

        assert (await rig.document(document)).status is DocumentStatus.PROCESSING
        assert await rig.store.claim_job("worker", lease_seconds=60) is None
        rig.clock.advance(29)
        assert await rig.store.claim_job("worker", lease_seconds=0) is None
        rig.clock.advance(1)
        job = await rig.work()
        assert job is not None
        assert job.attempts == 2
        assert (await rig.document(document)).status is DocumentStatus.READY

    async def test_each_wait_is_longer_than_the_last(self, rig: Rig) -> None:
        await rig.upload(PROSE)
        rig.embedder.failures = [GatewayUnavailableError("down")] * 3

        waits = []
        for expected in (30, 60, 120):
            await rig.work()
            rig.clock.advance(expected - 1)
            assert await rig.store.claim_job("worker", lease_seconds=0) is None
            rig.clock.advance(1)
            waits.append(expected)
        job = await rig.work()

        assert waits == [30, 60, 120]
        assert job is not None
        assert job.attempts == 4

    async def test_a_rate_limit_that_says_when_to_come_back_is_obeyed(self, rig: Rig) -> None:
        await rig.upload(PROSE)
        rig.embedder.failures = [RateLimitedError("slow down", retry_after_s=600)]

        await rig.work()

        rig.clock.advance(599)
        assert await rig.store.claim_job("worker", lease_seconds=0) is None
        rig.clock.advance(1)
        assert await rig.store.claim_job("worker", lease_seconds=60) is not None

    async def test_after_the_last_attempt_the_document_fails_and_says_so(self, rig: Rig) -> None:
        rig.configure(max_attempts=2)
        document = await rig.upload(PROSE)
        rig.embedder.failures = [GatewayUnavailableError("down")] * 2

        await rig.work()
        rig.clock.advance(30)
        await rig.work()

        failed = await rig.document(document)
        assert (failed.status, failed.error_code) == (
            DocumentStatus.FAILED,
            "embedding_unavailable",
        )
        assert failed.chunk_count == 0
        rig.clock.advance(100_000)
        assert await rig.store.claim_job("worker", lease_seconds=60) is None

    async def test_what_was_embedded_before_a_failure_is_not_embedded_again(self, rig: Rig) -> None:
        rig.configure(chunking=SMALL, batch=2)
        document = await rig.upload(PROSE)
        rig.embedder.failures = [None, GatewayUnavailableError("down")]

        await rig.work()
        assert (await rig.document(document)).chunks_embedded == 2
        rig.clock.advance(30)
        await rig.work()

        ready = await rig.document(document)
        assert ready.status is DocumentStatus.READY
        assert ready.chunks_embedded == ready.chunk_count
        # Every piece once, and the batch that failed a second time.
        assert len(rig.embedded_texts()) == ready.chunk_count + 2

    async def test_the_file_is_not_read_again_for_a_second_attempt(self, rig: Rig) -> None:
        rig.configure(chunking=SMALL, batch=2)
        await rig.upload(PROSE)
        rig.embedder.failures = [None, GatewayUnavailableError("down")]

        await rig.work()
        rig.clock.advance(30)
        await rig.work()

        assert rig.parses == [sniff.PLAIN]

    async def test_pieces_too_long_for_the_model_are_cut_smaller(self, rig: Rig) -> None:
        rig.embedder.max_chars = 300
        document = await rig.upload(PROSE)

        await rig.work()

        assert (await rig.document(document)).status is DocumentStatus.READY
        assert len(rig.embedder.calls) >= 3
        assert all(len(text) <= 300 for text in rig.embedder.calls[-1][1])

    async def test_a_second_attempt_that_finds_the_pieces_too_long_reads_the_file_again(
        self, rig: Rig
    ) -> None:
        document = await rig.upload(PROSE)
        rig.embedder.failures = [GatewayUnavailableError("down")]
        await rig.work()
        rig.embedder.max_chars = 300
        rig.clock.advance(30)

        await rig.work()

        assert (await rig.document(document)).status is DocumentStatus.READY
        assert rig.parses == [sniff.PLAIN, sniff.PLAIN]

    async def test_pieces_that_never_fit_fail_the_document(self, rig: Rig) -> None:
        rig.embedder.max_chars = 10
        document = await rig.upload(PROSE)

        await rig.work()

        failed = await rig.document(document)
        assert (failed.status, failed.error_code) == (DocumentStatus.FAILED, "context_too_long")
        assert failed.error_message is not None
        assert "smallest pieces" in failed.error_message

    async def test_a_model_that_changed_marks_the_collection_and_keeps_the_text_searchable(
        self, rig: Rig
    ) -> None:
        rig.embedder.space_id = "emb-another-model"
        document = await rig.upload(PROSE)

        await rig.work()

        ready = await rig.document(document)
        assert ready.status is DocumentStatus.READY
        assert ready.chunks_embedded == 0
        assert (await rig.the_collection()).status is CollectionStatus.NEEDS_REINDEX
        assert await rig.store.search_text(rig.tenant, [rig.collection.id], "router", limit=3)


class TestWhenSomethingGoesWrong:
    async def test_a_document_deleted_meanwhile_is_dropped_without_a_word(self, rig: Rig) -> None:
        document = await rig.upload(PROSE)

        async def delete() -> None:
            await rig.store.delete_document(rig.tenant, document.id)

        rig.embedder.during = delete

        job = await rig.work()

        assert job is not None
        assert await rig.store.claim_job("worker", lease_seconds=60) is None

    async def test_an_unexpected_failure_is_tried_again_and_then_fails_without_saying_what(
        self, rig: Rig
    ) -> None:
        rig.configure(max_attempts=2)

        async def broken(data: bytes, media_type: str, limits: Limits) -> ParsedDocument:
            raise RuntimeError("the secret contents of the document")

        rig.parser = broken
        rig.ingestor = rig.build()
        document = await rig.upload(PROSE)

        await rig.work()
        assert (await rig.document(document)).status is DocumentStatus.PROCESSING
        rig.clock.advance(30)
        await rig.work()

        failed = await rig.document(document)
        assert (failed.status, failed.error_code) == (DocumentStatus.FAILED, "internal_error")
        assert failed.error_message == "the document could not be processed"
        assert "secret" not in (failed.error_message or "")

    async def test_a_document_deleted_just_as_it_fails_is_not_failed_a_second_time(
        self, rig: Rig
    ) -> None:
        document = await rig.upload(PROSE)

        async def delete_and_fail() -> None:
            await rig.store.delete_document(rig.tenant, document.id)
            raise EmbeddingNotConfiguredError("no binding")

        rig.embedder.during = delete_and_fail

        job = await rig.work()

        assert job is not None
        assert await rig.store.claim_job("worker", lease_seconds=60) is None

    async def test_text_the_reader_finds_and_the_chunker_cannot_use_fails_the_document(
        self, rig: Rig
    ) -> None:
        async def only_blanks(data: bytes, media_type: str, limits: Limits) -> ParsedDocument:
            return ParsedDocument(None, (Block("text", "   "),), None)

        rig.parser = only_blanks
        rig.ingestor = rig.build()
        document = await rig.upload(PROSE)

        await rig.work()

        failed = await rig.document(document)
        assert failed.status is DocumentStatus.FAILED
        assert failed.error_message == "there is no text in the document to search"

    async def test_the_worker_stopped_meanwhile_leaves_the_job_to_another(self, rig: Rig) -> None:
        document = await rig.upload(PROSE)
        rig.embedder.failures = []

        async def stop() -> None:
            raise asyncio.CancelledError

        rig.embedder.during = stop

        with pytest.raises(asyncio.CancelledError):
            await rig.work()

        # Its lease runs out; the document was left as it was.
        assert (await rig.document(document)).status is DocumentStatus.PROCESSING
        rig.clock.advance(61)
        assert await rig.store.claim_job("another", lease_seconds=60) is not None


class TestReindexing:
    async def fill(self, rig: Rig) -> list[Document]:
        documents = [
            await rig.upload(PROSE, name="one.txt"),
            await rig.upload(MARKDOWN, name="two.md", media_type=sniff.MARKDOWN),
        ]
        await rig.work()
        await rig.work()
        return documents

    async def test_a_collection_is_embedded_again_with_the_model_its_owner_has_now(
        self, rig: Rig
    ) -> None:
        documents = await self.fill(rig)
        rig.embedder.space_id, rig.embedder.dimensions = "emb-new", 32
        await rig.store.mark_needs_reindex(rig.tenant, rig.collection.id)

        await rig.store.enqueue_reindex(rig.tenant, rig.collection.id)
        job = await rig.work()

        assert job is not None
        assert job.kind is JobKind.REINDEX
        collection = await rig.the_collection()
        assert collection.status is CollectionStatus.ACTIVE
        assert (collection.space_id, collection.dimensions) == ("emb-new", 32)
        for document in documents:
            again = await rig.document(document)
            assert again.status is DocumentStatus.READY
            assert again.chunks_embedded == again.chunk_count > 0
        hits = await rig.store.search_vector(
            rig.tenant, rig.collection.id, rig.embedder.vector("reset button"), limit=2
        )
        assert hits
        assert await rig.store.claim_job("worker", lease_seconds=60) is None

    async def test_a_reindex_that_cannot_begin_changes_nothing(self, rig: Rig) -> None:
        await self.fill(rig)
        before = await rig.the_collection()
        rig.embedder.failures = [EmbeddingNotConfiguredError("no binding")]

        await rig.store.enqueue_reindex(rig.tenant, rig.collection.id)
        await rig.work()

        assert await rig.the_collection() == before
        assert await rig.store.claim_job("worker", lease_seconds=60) is None

    async def test_a_reindex_that_stops_halfway_is_resumed_and_does_not_start_over(
        self, rig: Rig
    ) -> None:
        rig.configure(chunking=SMALL, batch=4)
        document = await rig.upload(PROSE)
        await rig.work()
        rig.embedder.space_id = "emb-new"
        rig.embedder.calls.clear()
        # The probe goes through, then one batch, then the gateway goes down.
        rig.embedder.failures = [None, None, GatewayUnavailableError("down")]

        await rig.store.enqueue_reindex(rig.tenant, rig.collection.id)
        await rig.work()
        assert (await rig.the_collection()).status is CollectionStatus.REINDEXING
        rig.clock.advance(30)
        await rig.work()

        ready = await rig.document(document)
        assert ready.chunks_embedded == ready.chunk_count
        assert (await rig.the_collection()).status is CollectionStatus.ACTIVE
        # The probe twice, every piece once, and the batch that failed a second time.
        assert len(rig.embedded_texts()) == 2 + ready.chunk_count + 4

    async def test_a_reindex_that_is_given_up_leaves_the_collection_marked(self, rig: Rig) -> None:
        rig.configure(max_attempts=1)
        await self.fill(rig)
        rig.embedder.space_id = "emb-new"
        rig.embedder.failures = [None, GatewayUnavailableError("down")]

        await rig.store.enqueue_reindex(rig.tenant, rig.collection.id)
        await rig.work()

        assert (await rig.the_collection()).status is CollectionStatus.NEEDS_REINDEX

    async def test_a_model_that_changes_once_more_during_a_reindex_leaves_it_marked(
        self, rig: Rig
    ) -> None:
        await self.fill(rig)
        rig.embedder.space_id = "emb-new"
        calls = 0

        async def change_again() -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                rig.embedder.space_id = "emb-newer"

        rig.embedder.during = change_again

        await rig.store.enqueue_reindex(rig.tenant, rig.collection.id)
        await rig.work()

        assert (await rig.the_collection()).status is CollectionStatus.NEEDS_REINDEX
        assert await rig.store.claim_job("worker", lease_seconds=60) is None

    async def test_a_collection_that_was_deleted_meanwhile_is_dropped_without_a_word(
        self, rig: Rig
    ) -> None:
        await self.fill(rig)
        await rig.store.enqueue_reindex(rig.tenant, rig.collection.id)

        async def delete() -> None:
            await rig.store.delete_collection(rig.tenant, rig.collection.id)

        rig.embedder.during = delete

        await rig.work()

        assert await rig.store.claim_job("worker", lease_seconds=60) is None
