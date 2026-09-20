"""What the service asks of its storage, and the shapes that come back.

Every method that touches a tenant's data takes the ``tenant_id`` first, and nothing crosses it: a
collection or a document of another tenant does not exist, whatever its id. Who *within* a tenant
may see a collection is decided by the service (``can_access``), not here.

Two implementations keep the same promises, checked by the same tests: Postgres, and one in memory
for the tests of everything above it.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from factotum.contracts.identity import Owner
from factotum.contracts.ids import ChunkId, CollectionId, DocumentId, TenantId
from factotum.contracts.knowledge import (
    Collection,
    CollectionUpdate,
    Document,
    DocumentStatus,
)
from factotum.knowledge.chunker import Chunks


class JobKind(StrEnum):
    INGEST = "ingest"
    """Read a document, cut it and embed the pieces."""

    REINDEX = "reindex"
    """Embed a whole collection again, with the embedding model its owner has now."""


@dataclass(frozen=True, slots=True)
class Job:
    id: UUID
    tenant_id: TenantId
    kind: JobKind
    collection_id: CollectionId
    document_id: DocumentId | None
    attempts: int
    """How many times it has been claimed, this time included. It also tells a claim from a later
    one: a worker whose lease ran out cannot finish the job somebody else is now doing."""


@dataclass(frozen=True, slots=True)
class NewDocument:
    collection_id: CollectionId
    title: str
    source: str | None
    media_type: str
    size_bytes: int
    content_sha256: str
    language: str | None
    language_declared: bool
    data: bytes


@dataclass(frozen=True, slots=True)
class PendingChunk:
    """A piece that still has to be embedded."""

    chunk_id: ChunkId
    document_id: DocumentId
    headings: tuple[str, ...]
    text: str


@dataclass(frozen=True, slots=True)
class Hit:
    """A piece a search found, with what is needed to show it."""

    chunk_id: ChunkId
    collection_id: CollectionId
    document_id: DocumentId
    parent_id: UUID
    document_title: str
    source: str | None
    text: str
    context: str
    headings: tuple[str, ...]
    page: int | None
    language: str | None
    score: float
    """How well it matched, in the terms of the search that found it: only meaningful for ordering
    the hits of that one search."""


class KnowledgeStore(Protocol):
    # ---- collections
    async def create_collection(
        self,
        tenant_id: TenantId,
        owner: Owner,
        *,
        name: str,
        description: str,
        space_id: str,
        dimensions: int,
    ) -> Collection:
        """Raises ``ConflictError`` when the owner already has a collection of that name."""
        ...

    async def get_collection(self, tenant_id: TenantId, collection_id: CollectionId) -> Collection:
        """Raises ``CollectionNotFoundError``."""
        ...

    async def list_collections(
        self, tenant_id: TenantId, *, counts: bool = True
    ) -> list[Collection]:
        """The collections of the tenant, oldest first. Without ``counts`` the number of documents
        and pieces and the languages are left out (zero and empty): counting them reads a large
        collection, and a search that only needs to know which collections there are does not
        need it."""
        ...

    async def update_collection(
        self, tenant_id: TenantId, collection_id: CollectionId, update: CollectionUpdate
    ) -> Collection: ...

    async def delete_collection(self, tenant_id: TenantId, collection_id: CollectionId) -> None:
        """With its documents, pieces and pending jobs."""
        ...

    async def mark_needs_reindex(self, tenant_id: TenantId, collection_id: CollectionId) -> None:
        """The embedding model is no longer the one that made the vectors (the ones it has, or the
        ones a re-embedding was making)."""
        ...

    async def begin_reindex(
        self, tenant_id: TenantId, collection_id: CollectionId, *, space_id: str, dimensions: int
    ) -> Collection:
        """Move the collection to a new space and forget its vectors, which are of the old one.
        Doing it again for the space it is already moving to changes nothing."""
        ...

    async def finish_reindex(
        self, tenant_id: TenantId, collection_id: CollectionId
    ) -> Collection: ...

    async def optimize(self, tenant_id: TenantId, collection_id: CollectionId) -> None:
        """Make a collection that has grown fast to search (an index). It can be called at any
        time and as often as wanted: it does nothing when there is nothing to do."""
        ...

    # ---- documents
    async def add_document(self, tenant_id: TenantId, new: NewDocument) -> Document:
        """Keep the file and queue it for reading. Raises ``CollectionNotFoundError``, and
        ``ConflictError`` when the collection already has this content."""
        ...

    async def get_document(self, tenant_id: TenantId, document_id: DocumentId) -> Document: ...

    async def list_documents(
        self, tenant_id: TenantId, collection_id: CollectionId, *, limit: int = 100, offset: int = 0
    ) -> list[Document]:
        """Newest first."""
        ...

    async def document_data(self, tenant_id: TenantId, document_id: DocumentId) -> bytes: ...

    async def delete_document(self, tenant_id: TenantId, document_id: DocumentId) -> None: ...

    async def retry_document(self, tenant_id: TenantId, document_id: DocumentId) -> Document:
        """Queue a document that failed to be read again. Raises ``ConflictError`` for one that
        did not fail."""
        ...

    async def set_document_state(
        self,
        tenant_id: TenantId,
        document_id: DocumentId,
        status: DocumentStatus,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
        language: str | None = None,
    ) -> None:
        """Move a document along. The error is kept only with ``FAILED``; a ``language`` is one that
        was detected, and never replaces one the uploader declared."""
        ...

    # ---- jobs (the tenant of a job is in the job: a worker serves every tenant)
    async def enqueue_reindex(self, tenant_id: TenantId, collection_id: CollectionId) -> None:
        """Queue a re-embedding of the collection, unless one is already waiting or running."""
        ...

    async def claim_job(self, worker: str, *, lease_seconds: float) -> Job | None:
        """The oldest job that is due, or whose worker stopped answering; for ``lease_seconds`` it
        is this worker's. Two workers never get the same one."""
        ...

    async def extend_job(self, job: Job, *, lease_seconds: float) -> None:
        """Say the worker is still at it: the job stays its own for ``lease_seconds`` from now."""
        ...

    async def finish_job(self, job: Job) -> None: ...

    async def retry_job(self, job: Job, *, delay_seconds: float) -> None: ...

    async def fail_job(self, job: Job, error: str) -> None: ...

    # ---- pieces
    async def replace_chunks(
        self, tenant_id: TenantId, document_id: DocumentId, chunks: Chunks
    ) -> None:
        """Keep exactly these pieces of the document, not yet embedded, in place of any it had."""
        ...

    async def pending_embeddings(
        self,
        tenant_id: TenantId,
        collection_id: CollectionId,
        *,
        limit: int,
        document_id: DocumentId | None = None,
    ) -> list[PendingChunk]:
        """The pieces of the collection that have no vector yet, in the order of the documents;
        only those of one document when it is given."""
        ...

    async def store_embeddings(
        self,
        tenant_id: TenantId,
        collection_id: CollectionId,
        items: Sequence[tuple[ChunkId, Sequence[float]]],
    ) -> None:
        """Raises ``ValueError`` for a vector that is not the size of the collection's. A piece that
        is gone (its document was deleted meanwhile) is skipped."""
        ...

    # ---- search
    async def search_vector(
        self,
        tenant_id: TenantId,
        collection_id: CollectionId,
        vector: Sequence[float],
        *,
        limit: int,
    ) -> list[Hit]:
        """The nearest pieces that have a vector, nearest first."""
        ...

    async def search_text(
        self,
        tenant_id: TenantId,
        collection_ids: Sequence[CollectionId],
        query: str,
        *,
        limit: int,
    ) -> list[Hit]:
        """The pieces that have some of the words of the query, best first; each document is
        searched in its own language."""
        ...
