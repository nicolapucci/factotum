"""Support for the tests of the service: things to put in a store, and a way to empty a database."""

import hashlib
import math
import re
import unicodedata
import zlib
from collections.abc import Awaitable, Callable, Sequence

from factotum.contracts.errors import ContextTooLongError
from factotum.contracts.identity import Owner
from factotum.contracts.ids import ChunkId, CollectionId, TenantId
from factotum.contracts.knowledge import Collection, Document
from factotum.db.pool import Database
from factotum.knowledge.chunker import Child, Chunks, Parent
from factotum.knowledge.embedder import Embedded
from factotum.knowledge.ids import chunk_id
from factotum.knowledge.store import KnowledgeStore, NewDocument


async def make_collection(
    store: KnowledgeStore,
    tenant: TenantId,
    owner: Owner,
    name: str = "Manuals",
    dimensions: int = 4,
    space_id: str = "emb-one",
) -> Collection:
    return await store.create_collection(
        tenant, owner, name=name, description="", space_id=space_id, dimensions=dimensions
    )


def new_document(
    collection_id: CollectionId,
    content: bytes = b"content",
    *,
    title: str = "Router manual",
    language: str | None = None,
    declared: bool = False,
    source: str | None = "router.pdf",
) -> NewDocument:
    return NewDocument(
        collection_id=collection_id,
        title=title,
        source=source,
        media_type="application/pdf",
        size_bytes=len(content),
        content_sha256=hashlib.sha256(content).hexdigest(),
        language=language,
        language_declared=declared,
        data=content,
    )


def pieces(*texts: str, headings: tuple[str, ...] = (), page: int | None = None) -> Chunks:
    """Each text is a child of a parent of its own."""
    return Chunks(
        tuple(Parent(i, text, headings, page) for i, text in enumerate(texts)),
        tuple(Child(i, i, text, headings, page) for i, text in enumerate(texts)),
    )


def family(
    context: str, *texts: str, headings: tuple[str, ...] = (), page: int | None = None
) -> Chunks:
    """Children of one parent."""
    return Chunks(
        (Parent(0, context, headings, page),),
        tuple(Child(i, 0, text, headings, page) for i, text in enumerate(texts)),
    )


async def indexed(
    store: KnowledgeStore,
    tenant: TenantId,
    collection: Collection,
    document: Document,
    chunks: Chunks,
    vectors: list[tuple[float, ...]] | None = None,
) -> list[ChunkId]:
    """Give the document these pieces, and vectors for them when given."""
    await store.replace_chunks(tenant, document.id, chunks)
    if vectors is not None:
        pending = await store.pending_embeddings(tenant, collection.id, limit=100_000)
        mine = [p for p in pending if p.document_id == document.id]
        # ``strict``: one vector for each piece of the document, no more and no fewer.
        await store.store_embeddings(
            tenant, collection.id, [(p.chunk_id, v) for p, v in zip(mine, vectors, strict=True)]
        )
    return [chunk_id(document.id, child.ordinal) for child in chunks.children]


async def wipe(database: Database) -> None:
    """Empty the service's tables, and drop the indexes made for single collections."""
    async with database.connection() as connection:
        indexes = await connection.fetch(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = 'knowledge' AND indexname LIKE 'knowledge_chunks_vec_%'"
        )
        for index in indexes:
            await connection.execute(f'DROP INDEX knowledge."{index["indexname"]}"')
        await connection.execute(
            "TRUNCATE knowledge.jobs, knowledge.chunks, knowledge.parents, "
            "knowledge.document_data, knowledge.documents, knowledge.collections"
        )


class FakeEmbedder:
    """An embedder that needs no model. A word makes a direction, so texts that share words are near
    each other and a search by meaning finds something. It can be told to fail, or to refuse texts
    that are too long, and it keeps what it was asked."""

    def __init__(self, *, space_id: str = "emb-fake", dimensions: int = 64) -> None:
        self.space_id = space_id
        self.dimensions = dimensions
        self.calls: list[tuple[Owner, list[str]]] = []
        self.failures: list[Exception | None] = []
        """One for each call, in turn: an error is raised, ``None`` lets the call through."""

        self.max_chars: int | None = None
        """A text longer than this does not fit the model."""

        self.during: Callable[[], Awaitable[None]] | None = None
        """Something that happens while a call is being answered."""

    async def embed(self, owner: Owner, texts: Sequence[str]) -> Embedded:
        self.calls.append((owner, list(texts)))
        if self.during is not None:
            await self.during()
        if self.failures:
            failure = self.failures.pop(0)
            if failure is not None:
                raise failure
        if self.max_chars is not None and any(len(text) > self.max_chars for text in texts):
            raise ContextTooLongError("a text is too long for the model")
        return Embedded(tuple(self.vector(text) for text in texts), self.space_id, self.dimensions)

    def vector(self, text: str) -> tuple[float, ...]:
        folded = unicodedata.normalize("NFKD", text.casefold())
        plain = "".join(char for char in folded if not unicodedata.combining(char))
        counts = [0.0] * self.dimensions
        for word in re.findall(r"\w+", plain):
            counts[zlib.crc32(word.encode()) % self.dimensions] += 1.0
        norm = math.sqrt(sum(count * count for count in counts))
        if norm == 0:
            counts[0], norm = 1.0, 1.0
        return tuple(count / norm for count in counts)
