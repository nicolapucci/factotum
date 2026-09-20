"""Support for the tests of the service: things to put in a store, and a way to empty a database."""

import hashlib

from factotum.contracts.identity import Owner
from factotum.contracts.ids import ChunkId, CollectionId, TenantId
from factotum.contracts.knowledge import Collection, Document
from factotum.db.pool import Database
from factotum.knowledge.chunker import Child, Chunks, Parent
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
