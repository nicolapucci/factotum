"""The store in memory: for the tests of everything above the database.

It keeps the promises of the interface and nothing more. Its text search has no stemming (Postgres
has), so a test that depends on stemming belongs to the Postgres tests.
"""

import itertools
import math
import re
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from factotum.contracts.errors import (
    CollectionNotFoundError,
    ConflictError,
    DocumentNotFoundError,
)
from factotum.contracts.identity import Owner
from factotum.contracts.ids import ChunkId, CollectionId, DocumentId, TenantId
from factotum.contracts.knowledge import (
    Collection,
    CollectionStatus,
    CollectionUpdate,
    Document,
    DocumentStatus,
)
from factotum.knowledge.chunker import Chunks
from factotum.knowledge.ids import chunk_id, parent_id
from factotum.knowledge.store.base import Hit, Job, JobKind, NewDocument, PendingChunk

_TOKEN = re.compile(r"\w+")


def _tokens(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(char for char in folded if not unicodedata.combining(char))
    return [token for token in _TOKEN.findall(plain) if len(token) > 1]


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class _Collection:
    id: CollectionId
    tenant_id: TenantId
    owner: Owner
    name: str
    description: str
    space_id: str
    dimensions: int
    status: CollectionStatus = CollectionStatus.ACTIVE
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)


@dataclass
class _Document:
    id: DocumentId
    tenant_id: TenantId
    collection_id: CollectionId
    title: str
    source: str | None
    media_type: str
    size_bytes: int
    content_sha256: str
    language: str | None
    language_declared: bool
    data: bytes
    sequence: int
    status: DocumentStatus = DocumentStatus.PENDING
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)


@dataclass
class _Parent:
    id: UUID
    text: str


@dataclass
class _Chunk:
    id: ChunkId
    tenant_id: TenantId
    collection_id: CollectionId
    document_id: DocumentId
    parent_id: UUID
    ordinal: int
    text: str
    headings: tuple[str, ...]
    page: int | None
    embedding: tuple[float, ...] | None = None


@dataclass
class _Job:
    id: UUID
    tenant_id: TenantId
    kind: JobKind
    collection_id: CollectionId
    document_id: DocumentId | None
    sequence: int
    state: str = "queued"  # queued, running, failed
    attempts: int = 0
    due_at: float = 0.0
    lease_until: float = 0.0
    error: str | None = None


class InMemoryStore:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._counter = itertools.count()
        self._collections: dict[CollectionId, _Collection] = {}
        self._documents: dict[DocumentId, _Document] = {}
        self._parents: dict[UUID, _Parent] = {}
        self._chunks: dict[ChunkId, _Chunk] = {}
        self._jobs: dict[UUID, _Job] = {}

    # ---- lookups
    def _collection(self, tenant_id: TenantId, collection_id: CollectionId) -> _Collection:
        found = self._collections.get(collection_id)
        if found is None or found.tenant_id != tenant_id:
            raise CollectionNotFoundError("the collection does not exist")
        return found

    def _document(self, tenant_id: TenantId, document_id: DocumentId) -> _Document:
        found = self._documents.get(document_id)
        if found is None or found.tenant_id != tenant_id:
            raise DocumentNotFoundError("the document does not exist")
        return found

    def _chunks_of(self, document_id: DocumentId) -> list[_Chunk]:
        return sorted(
            (c for c in self._chunks.values() if c.document_id == document_id),
            key=lambda c: c.ordinal,
        )

    # ---- views
    def _view_collection(self, found: _Collection) -> Collection:
        documents = [d for d in self._documents.values() if d.collection_id == found.id]
        languages = sorted({d.language for d in documents if d.language})
        return Collection(
            id=found.id,
            tenant_id=found.tenant_id,
            owner=found.owner,
            name=found.name,
            description=found.description,
            space_id=found.space_id,
            dimensions=found.dimensions,
            status=found.status,
            languages=tuple(languages),
            document_count=len(documents),
            chunk_count=sum(1 for c in self._chunks.values() if c.collection_id == found.id),
            created_at=found.created_at,
            updated_at=found.updated_at,
        )

    def _view_document(self, found: _Document) -> Document:
        chunks = self._chunks_of(found.id)
        return Document(
            id=found.id,
            collection_id=found.collection_id,
            title=found.title,
            source=found.source,
            media_type=found.media_type,
            size_bytes=found.size_bytes,
            content_sha256=found.content_sha256,
            language=found.language,
            language_declared=found.language_declared,
            status=found.status,
            error_code=found.error_code,
            error_message=found.error_message,
            chunk_count=len(chunks),
            chunks_embedded=sum(1 for c in chunks if c.embedding is not None),
            created_at=found.created_at,
            updated_at=found.updated_at,
        )

    def _name_taken(
        self, tenant_id: TenantId, owner: Owner, name: str, *, except_id: CollectionId | None
    ) -> bool:
        return any(
            c.tenant_id == tenant_id
            and c.owner == owner
            and c.name.casefold() == name.casefold()
            and c.id != except_id
            for c in self._collections.values()
        )

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
        if self._name_taken(tenant_id, owner, name, except_id=None):
            raise ConflictError("a collection with that name already exists")
        found = _Collection(
            CollectionId(uuid4()), tenant_id, owner, name, description, space_id, dimensions
        )
        self._collections[found.id] = found
        return self._view_collection(found)

    async def get_collection(self, tenant_id: TenantId, collection_id: CollectionId) -> Collection:
        return self._view_collection(self._collection(tenant_id, collection_id))

    async def list_collections(self, tenant_id: TenantId) -> list[Collection]:
        mine = [c for c in self._collections.values() if c.tenant_id == tenant_id]
        return [
            self._view_collection(c) for c in sorted(mine, key=lambda c: (c.created_at, c.name))
        ]

    async def update_collection(
        self, tenant_id: TenantId, collection_id: CollectionId, update: CollectionUpdate
    ) -> Collection:
        found = self._collection(tenant_id, collection_id)
        if update.name is not None:
            if self._name_taken(tenant_id, found.owner, update.name, except_id=found.id):
                raise ConflictError("a collection with that name already exists")
            found.name = update.name
        if update.description is not None:
            found.description = update.description
        found.updated_at = _now()
        return self._view_collection(found)

    async def delete_collection(self, tenant_id: TenantId, collection_id: CollectionId) -> None:
        found = self._collection(tenant_id, collection_id)
        for document in [d for d in self._documents.values() if d.collection_id == found.id]:
            self._drop_document(document)
        for job in [j for j in self._jobs.values() if j.collection_id == found.id]:
            del self._jobs[job.id]
        del self._collections[found.id]

    async def mark_needs_reindex(self, tenant_id: TenantId, collection_id: CollectionId) -> None:
        found = self._collection(tenant_id, collection_id)
        if found.status is not CollectionStatus.NEEDS_REINDEX:
            found.status = CollectionStatus.NEEDS_REINDEX
            found.updated_at = _now()

    async def begin_reindex(
        self, tenant_id: TenantId, collection_id: CollectionId, *, space_id: str, dimensions: int
    ) -> Collection:
        found = self._collection(tenant_id, collection_id)
        already = (
            found.status is CollectionStatus.REINDEXING
            and found.space_id == space_id
            and found.dimensions == dimensions
        )
        if not already:
            found.space_id, found.dimensions = space_id, dimensions
            found.status = CollectionStatus.REINDEXING
            found.updated_at = _now()
            for chunk in self._chunks.values():
                if chunk.collection_id == found.id:
                    chunk.embedding = None
        return self._view_collection(found)

    async def finish_reindex(self, tenant_id: TenantId, collection_id: CollectionId) -> Collection:
        found = self._collection(tenant_id, collection_id)
        found.status = CollectionStatus.ACTIVE
        found.updated_at = _now()
        return self._view_collection(found)

    async def optimize(self, tenant_id: TenantId, collection_id: CollectionId) -> None:
        self._collection(tenant_id, collection_id)  # nothing to index: it is searched as it is

    # ---- documents
    def _drop_document(self, document: _Document) -> None:
        for chunk in self._chunks_of(document.id):
            del self._chunks[chunk.id]
        self._forget_parents()
        for job in [j for j in self._jobs.values() if j.document_id == document.id]:
            del self._jobs[job.id]
        del self._documents[document.id]

    def _forget_parents(self) -> None:
        used = {chunk.parent_id for chunk in self._chunks.values()}
        for orphan in [p for p in self._parents if p not in used]:
            del self._parents[orphan]

    async def add_document(self, tenant_id: TenantId, new: NewDocument) -> Document:
        collection = self._collection(tenant_id, new.collection_id)
        if any(
            d.collection_id == collection.id and d.content_sha256 == new.content_sha256
            for d in self._documents.values()
        ):
            raise ConflictError("the collection already has this document")
        found = _Document(
            id=DocumentId(uuid4()),
            tenant_id=tenant_id,
            collection_id=collection.id,
            title=new.title,
            source=new.source,
            media_type=new.media_type,
            size_bytes=new.size_bytes,
            content_sha256=new.content_sha256,
            language=new.language,
            language_declared=new.language_declared,
            data=new.data,
            sequence=next(self._counter),
        )
        self._documents[found.id] = found
        self._enqueue(tenant_id, JobKind.INGEST, collection.id, found.id)
        return self._view_document(found)

    async def get_document(self, tenant_id: TenantId, document_id: DocumentId) -> Document:
        return self._view_document(self._document(tenant_id, document_id))

    async def list_documents(
        self, tenant_id: TenantId, collection_id: CollectionId, *, limit: int = 100, offset: int = 0
    ) -> list[Document]:
        collection = self._collection(tenant_id, collection_id)
        mine = [d for d in self._documents.values() if d.collection_id == collection.id]
        newest_first = sorted(mine, key=lambda d: d.sequence, reverse=True)
        return [self._view_document(d) for d in newest_first[offset : offset + limit]]

    async def document_data(self, tenant_id: TenantId, document_id: DocumentId) -> bytes:
        return self._document(tenant_id, document_id).data

    async def delete_document(self, tenant_id: TenantId, document_id: DocumentId) -> None:
        self._drop_document(self._document(tenant_id, document_id))

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
        found = self._document(tenant_id, document_id)
        found.status = status
        failed = status is DocumentStatus.FAILED
        found.error_code = error_code if failed else None
        found.error_message = error_message if failed else None
        if language is not None and not found.language_declared:
            found.language = language
        found.updated_at = _now()

    # ---- jobs
    def _enqueue(
        self,
        tenant_id: TenantId,
        kind: JobKind,
        collection_id: CollectionId,
        document_id: DocumentId | None,
    ) -> None:
        job = _Job(uuid4(), tenant_id, kind, collection_id, document_id, next(self._counter))
        job.due_at = self._clock()
        self._jobs[job.id] = job

    async def enqueue_reindex(self, tenant_id: TenantId, collection_id: CollectionId) -> None:
        collection = self._collection(tenant_id, collection_id)
        if any(
            j.kind is JobKind.REINDEX and j.collection_id == collection.id and j.state != "failed"
            for j in self._jobs.values()
        ):
            return
        self._enqueue(tenant_id, JobKind.REINDEX, collection.id, None)

    async def claim_job(self, worker: str, *, lease_seconds: float) -> Job | None:
        now = self._clock()
        due = [
            j
            for j in self._jobs.values()
            if (j.state == "queued" and j.due_at <= now)
            or (j.state == "running" and j.lease_until <= now)
        ]
        if not due:
            return None
        chosen = min(due, key=lambda j: (j.due_at, j.sequence))
        chosen.state = "running"
        chosen.attempts += 1
        chosen.lease_until = now + lease_seconds
        return self._as_job(chosen)

    @staticmethod
    def _as_job(job: _Job) -> Job:
        return Job(
            job.id, job.tenant_id, job.kind, job.collection_id, job.document_id, job.attempts
        )

    def _mine(self, job: Job) -> _Job | None:
        """The stored job, if it is still the one this claim was for."""
        found = self._jobs.get(job.id)
        return found if found is not None and found.attempts == job.attempts else None

    async def extend_job(self, job: Job, *, lease_seconds: float) -> None:
        found = self._mine(job)
        if found is not None and found.state == "running":
            found.lease_until = self._clock() + lease_seconds

    async def finish_job(self, job: Job) -> None:
        if self._mine(job) is not None:
            del self._jobs[job.id]

    async def retry_job(self, job: Job, *, delay_seconds: float) -> None:
        found = self._mine(job)
        if found is not None:
            found.state = "queued"
            found.due_at = self._clock() + delay_seconds

    async def fail_job(self, job: Job, error: str) -> None:
        found = self._mine(job)
        if found is not None:
            found.state = "failed"
            found.error = error

    # ---- pieces
    async def replace_chunks(
        self, tenant_id: TenantId, document_id: DocumentId, chunks: Chunks
    ) -> None:
        document = self._document(tenant_id, document_id)
        for old in self._chunks_of(document.id):
            del self._chunks[old.id]
        for parent in chunks.parents:
            identity = parent_id(document.id, parent.ordinal)
            self._parents[identity] = _Parent(identity, parent.text)
        for child in chunks.children:
            identity = chunk_id(document.id, child.ordinal)
            self._chunks[identity] = _Chunk(
                id=identity,
                tenant_id=tenant_id,
                collection_id=document.collection_id,
                document_id=document.id,
                parent_id=parent_id(document.id, child.parent),
                ordinal=child.ordinal,
                text=child.text,
                headings=child.headings,
                page=child.page,
            )
        self._forget_parents()
        document.updated_at = _now()

    async def pending_embeddings(
        self,
        tenant_id: TenantId,
        collection_id: CollectionId,
        *,
        limit: int,
        document_id: DocumentId | None = None,
    ) -> list[PendingChunk]:
        collection = self._collection(tenant_id, collection_id)
        ordered = sorted(
            (
                c
                for c in self._chunks.values()
                if c.collection_id == collection.id
                and c.embedding is None
                and (document_id is None or c.document_id == document_id)
            ),
            key=lambda c: (self._documents[c.document_id].sequence, c.ordinal),
        )
        return [PendingChunk(c.id, c.document_id, c.headings, c.text) for c in ordered[:limit]]

    async def store_embeddings(
        self,
        tenant_id: TenantId,
        collection_id: CollectionId,
        items: Sequence[tuple[ChunkId, Sequence[float]]],
    ) -> None:
        collection = self._collection(tenant_id, collection_id)
        for _, vector in items:
            if len(vector) != collection.dimensions:
                raise ValueError(
                    f"a vector of {len(vector)} numbers does not fit a collection "
                    f"of {collection.dimensions}"
                )
        for identity, vector in items:
            chunk = self._chunks.get(identity)
            if chunk is not None and chunk.collection_id == collection.id:
                chunk.embedding = tuple(vector)

    # ---- search
    def _hit(self, chunk: _Chunk, score: float) -> Hit:
        document = self._documents[chunk.document_id]
        return Hit(
            chunk_id=chunk.id,
            collection_id=chunk.collection_id,
            document_id=chunk.document_id,
            parent_id=chunk.parent_id,
            document_title=document.title,
            source=document.source,
            text=chunk.text,
            context=self._parents[chunk.parent_id].text,
            headings=chunk.headings,
            page=chunk.page,
            language=document.language,
            score=score,
        )

    async def search_vector(
        self,
        tenant_id: TenantId,
        collection_id: CollectionId,
        vector: Sequence[float],
        *,
        limit: int,
    ) -> list[Hit]:
        collection = self._collection(tenant_id, collection_id)
        if len(vector) != collection.dimensions:
            raise ValueError("the query vector does not fit the collection")
        scored = [
            (_cosine(vector, chunk.embedding), chunk)
            for chunk in self._chunks.values()
            if chunk.collection_id == collection.id and chunk.embedding is not None
        ]
        scored.sort(
            key=lambda pair: (
                -pair[0],
                self._documents[pair[1].document_id].sequence,
                pair[1].ordinal,
            )
        )
        return [self._hit(chunk, score) for score, chunk in scored[:limit]]

    async def search_text(
        self,
        tenant_id: TenantId,
        collection_ids: Sequence[CollectionId],
        query: str,
        *,
        limit: int,
    ) -> list[Hit]:
        wanted = {self._collection(tenant_id, cid).id for cid in collection_ids}
        words = set(_tokens(query))
        candidates = [c for c in self._chunks.values() if c.collection_id in wanted]
        if not words or not candidates:
            return []
        indexed = [(c, Counter(_tokens(f"{' '.join(c.headings)} {c.text}"))) for c in candidates]
        frequency = Counter(word for _, counts in indexed for word in counts)
        total = len(indexed)
        scored: list[tuple[float, _Chunk]] = []
        for chunk, counts in indexed:
            score = sum(
                math.log(1 + total / frequency[word]) * (1 + math.log(counts[word]))
                for word in words
                if word in counts
            )
            if score > 0:
                scored.append((score, chunk))
        scored.sort(
            key=lambda pair: (
                -pair[0],
                self._documents[pair[1].document_id].sequence,
                pair[1].ordinal,
            )
        )
        return [self._hit(chunk, score) for score, chunk in scored[:limit]]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0
