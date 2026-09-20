"""The store in Postgres: pgvector for meaning, full-text search for words.

**Meaning.** A collection's vectors all have the size of its embedding model, and the column has no
size of its own, so the index that makes a large collection fast is made per collection, for its
size: a partial HNSW index on the column cast to that size (``halfvec`` above 2000 numbers, which
is as far as ``vector`` can be indexed; nothing above 4000). Queries are written with the same
cast and the collection as a literal, because that is how the planner recognizes the index.

Building an index scans the whole table and a small collection does not need one: below
``index_threshold`` pieces the search is exact, which is fast and never misses. The index is made
when a collection grows past it (``optimize``), without blocking anybody else.

**Words.** Each piece keeps the text search configuration of its document's language (stemming and
stop words differ), and a query is read once for each language present in the collections it
searches. Its words are OR-ed and the pieces ranked by how many they have: a question is not a
list of keywords that must all be there.
"""

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

import asyncpg

from factotum.contracts.errors import (
    CollectionNotFoundError,
    ConflictError,
    DocumentNotFoundError,
)
from factotum.contracts.identity import Owner, OwnerKind
from factotum.contracts.ids import ChunkId, CollectionId, DocumentId, TenantId
from factotum.contracts.knowledge import (
    Collection,
    CollectionStatus,
    CollectionUpdate,
    Document,
    DocumentStatus,
)
from factotum.db.migrations import load_migrations, migrate
from factotum.db.pool import Database
from factotum.db.types import Connection
from factotum.knowledge.chunker import Chunks, breadcrumb
from factotum.knowledge.fusion import reciprocal_rank_fusion
from factotum.knowledge.ids import chunk_id, parent_id
from factotum.knowledge.language import fts_config
from factotum.knowledge.store.base import Hit, Job, JobKind, NewDocument, PendingChunk

SCHEMA = "knowledge"
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"

DEFAULT_INDEX_THRESHOLD = 2000
"""Pieces with a vector, from which a collection gets an index."""

_VECTOR_INDEX_LIMIT = 2000
_HALFVEC_INDEX_LIMIT = 4000
_MIN_EF_SEARCH = 40
_MAX_EF_SEARCH = 1000
_BUILD_MEMORY = "256MB"
_MAX_ERROR_CHARS = 2000


@dataclass(frozen=True, slots=True)
class VectorForm:
    """How the vectors of a collection are written in SQL. The index and the queries must say it
    the same way, or the planner does not use the index."""

    dimensions: int

    @property
    def type_name(self) -> str:
        return "vector" if self.dimensions <= _VECTOR_INDEX_LIMIT else "halfvec"

    @property
    def indexable(self) -> bool:
        return self.dimensions <= _HALFVEC_INDEX_LIMIT

    @property
    def operator_class(self) -> str:
        return f"{self.type_name}_cosine_ops"

    def column(self, alias: str = "") -> str:
        prefix = f"{alias}." if alias else ""
        return f"{prefix}embedding::{self.type_name}({self.dimensions})"

    def parameter(self, number: int) -> str:
        return f"${number}::text::{self.type_name}({self.dimensions})"


_SHOW = (
    "SELECT n.id, n.collection_id, n.document_id, n.parent_id, n.text, n.headings, n.page, "
    "d.title, d.source, d.language, p.text AS context, 1 - n.distance AS score "
    "FROM nearest n "
    "JOIN knowledge.documents d ON d.id = n.document_id "
    "JOIN knowledge.parents p ON p.id = n.parent_id"
)


async def prepare(database: Database) -> list[int]:
    """Bring the tables of the service up to date. Returns the migrations that were applied."""
    async with database.connection() as connection:
        return await migrate(connection, SCHEMA, load_migrations(MIGRATIONS))


def vector_index_name(collection_id: CollectionId) -> str:
    return f"knowledge_chunks_vec_{collection_id.hex}"


def vector_index_sql(collection_id: CollectionId, dimensions: int) -> str:
    form = VectorForm(dimensions)
    return (
        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {vector_index_name(collection_id)} "
        f"ON knowledge.chunks USING hnsw (({form.column()}) {form.operator_class}) "
        f"WHERE collection_id = '{collection_id}'"
    )


def vector_search_sql(collection_id: CollectionId, dimensions: int) -> str:
    """The search by meaning of one collection. Parameters: tenant, the query vector as text,
    how many. The pieces are found first, in a query the index can answer, and shown after."""
    form = VectorForm(dimensions)
    distance = f"{form.column('c')} <=> {form.parameter(2)}"
    return (
        "WITH nearest AS ("  # noqa: S608 - the collection is a UUID and the size an integer
        "SELECT c.id, c.collection_id, c.document_id, c.parent_id, c.ordinal, c.text, c.headings,"
        f" c.page, {distance} AS distance "
        "FROM knowledge.chunks c "
        f"WHERE c.collection_id = '{collection_id}' AND c.tenant_id = $1 "
        "AND c.embedding IS NOT NULL "
        f"ORDER BY {distance} LIMIT $3) "
        f"{_SHOW} ORDER BY n.distance, d.seq, n.ordinal"
    )


TEXT_SEARCH_SQL = """
WITH q AS (
    SELECT replace(plainto_tsquery($4::text::regconfig, $3)::text, ' & ', ' | ')::tsquery AS query
)
SELECT c.id, c.collection_id, c.document_id, c.parent_id, c.text, c.headings, c.page,
       d.title, d.source, d.language, p.text AS context, ts_rank(c.tsv, q.query) AS score
FROM knowledge.chunks c
CROSS JOIN q
JOIN knowledge.documents d ON d.id = c.document_id
JOIN knowledge.parents p ON p.id = c.parent_id
WHERE c.tenant_id = $1 AND c.collection_id = ANY($2::uuid[]) AND c.fts_config = $5
  AND c.tsv @@ q.query
ORDER BY score DESC, d.seq, c.ordinal
LIMIT $6
"""

_COLLECTION = """
SELECT c.id, c.tenant_id, c.owner_kind, c.owner_id, c.name, c.description, c.space_id,
       c.dimensions, c.status, c.created_at, c.updated_at,
       (SELECT count(*) FROM knowledge.documents d WHERE d.collection_id = c.id) AS document_count,
       (SELECT count(*) FROM knowledge.chunks k WHERE k.collection_id = c.id) AS chunk_count,
       COALESCE(
           (SELECT array_agg(DISTINCT d.language ORDER BY d.language)
              FROM knowledge.documents d
             WHERE d.collection_id = c.id AND d.language IS NOT NULL),
           ARRAY[]::text[]
       ) AS languages
FROM knowledge.collections c
"""

_COLLECTION_ROW = (
    "SELECT id, space_id, dimensions, status FROM knowledge.collections "
    "WHERE tenant_id = $1 AND id = $2"
)
_COLLECTION_ROW_LOCKED = _COLLECTION_ROW + " FOR UPDATE"

_DOCUMENT = """
SELECT d.id, d.collection_id, d.title, d.source, d.media_type, d.size_bytes, d.content_sha256,
       d.language, d.language_declared, d.status, d.error_code, d.error_message,
       d.created_at, d.updated_at,
       (SELECT count(*) FROM knowledge.chunks c WHERE c.document_id = d.id) AS chunk_count,
       (SELECT count(*) FROM knowledge.chunks c
         WHERE c.document_id = d.id AND c.embedding IS NOT NULL) AS chunks_embedded
FROM knowledge.documents d
"""


def vector_text(vector: Sequence[float]) -> str:
    """A vector as pgvector reads it. Only finite numbers are vectors."""
    if not all(math.isfinite(number) for number in vector):
        raise ValueError("a vector holds only finite numbers")
    return "[" + ",".join(repr(float(number)) for number in vector) + "]"


def _collection(row: asyncpg.Record) -> Collection:
    return Collection(
        id=row["id"],
        tenant_id=row["tenant_id"],
        owner=Owner(kind=OwnerKind(row["owner_kind"]), id=row["owner_id"]),
        name=row["name"],
        description=row["description"],
        space_id=row["space_id"],
        dimensions=row["dimensions"],
        status=CollectionStatus(row["status"]),
        languages=tuple(row["languages"]),
        document_count=row["document_count"],
        chunk_count=row["chunk_count"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _document(row: asyncpg.Record) -> Document:
    return Document(
        id=row["id"],
        collection_id=row["collection_id"],
        title=row["title"],
        source=row["source"],
        media_type=row["media_type"],
        size_bytes=row["size_bytes"],
        content_sha256=row["content_sha256"],
        language=row["language"],
        language_declared=row["language_declared"],
        status=DocumentStatus(row["status"]),
        error_code=row["error_code"],
        error_message=row["error_message"],
        chunk_count=row["chunk_count"],
        chunks_embedded=row["chunks_embedded"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _hit(row: asyncpg.Record) -> Hit:
    return Hit(
        chunk_id=ChunkId(row["id"]),
        collection_id=CollectionId(row["collection_id"]),
        document_id=DocumentId(row["document_id"]),
        parent_id=row["parent_id"],
        document_title=row["title"],
        source=row["source"],
        text=row["text"],
        context=row["context"],
        headings=tuple(json.loads(row["headings"])),
        page=row["page"],
        language=row["language"],
        score=float(row["score"]),
    )


async def _collection_row(
    connection: Connection,
    tenant_id: TenantId,
    collection_id: CollectionId,
    *,
    lock: bool = False,
) -> asyncpg.Record:
    row = await connection.fetchrow(
        _COLLECTION_ROW_LOCKED if lock else _COLLECTION_ROW, tenant_id, collection_id
    )
    if row is None:
        raise CollectionNotFoundError("the collection does not exist")
    return row


class PostgresStore:
    def __init__(
        self, database: Database, *, index_threshold: int = DEFAULT_INDEX_THRESHOLD
    ) -> None:
        self._db = database
        self._index_threshold = index_threshold

    @property
    def database(self) -> Database:
        return self._db

    # ---- collections
    async def _read_collection(
        self, connection: Connection, tenant_id: TenantId, collection_id: CollectionId
    ) -> Collection:
        row = await connection.fetchrow(
            f"{_COLLECTION} WHERE c.tenant_id = $1 AND c.id = $2", tenant_id, collection_id
        )
        if row is None:
            raise CollectionNotFoundError("the collection does not exist")
        return _collection(row)

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
        collection_id = CollectionId(uuid4())
        async with self._db.transaction(tenant_id) as connection:
            try:
                await connection.execute(
                    "INSERT INTO knowledge.collections (id, tenant_id, owner_kind, owner_id, "
                    "name, description, space_id, dimensions) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                    collection_id,
                    tenant_id,
                    owner.kind.value,
                    owner.id,
                    name,
                    description,
                    space_id,
                    dimensions,
                )
            except asyncpg.UniqueViolationError:
                raise ConflictError("a collection with that name already exists") from None
            return await self._read_collection(connection, tenant_id, collection_id)

    async def get_collection(self, tenant_id: TenantId, collection_id: CollectionId) -> Collection:
        async with self._db.transaction(tenant_id) as connection:
            return await self._read_collection(connection, tenant_id, collection_id)

    async def list_collections(self, tenant_id: TenantId) -> list[Collection]:
        async with self._db.transaction(tenant_id) as connection:
            rows = await connection.fetch(
                f"{_COLLECTION} WHERE c.tenant_id = $1 ORDER BY c.created_at, c.name, c.id",
                tenant_id,
            )
        return [_collection(row) for row in rows]

    async def update_collection(
        self, tenant_id: TenantId, collection_id: CollectionId, update: CollectionUpdate
    ) -> Collection:
        async with self._db.transaction(tenant_id) as connection:
            try:
                changed = await connection.fetchval(
                    "UPDATE knowledge.collections SET name = COALESCE($3, name), "
                    "description = COALESCE($4, description), updated_at = now() "
                    "WHERE tenant_id = $1 AND id = $2 RETURNING id",
                    tenant_id,
                    collection_id,
                    update.name,
                    update.description,
                )
            except asyncpg.UniqueViolationError:
                raise ConflictError("a collection with that name already exists") from None
            if changed is None:
                raise CollectionNotFoundError("the collection does not exist")
            return await self._read_collection(connection, tenant_id, collection_id)

    async def _drop_vector_index(self, collection_id: CollectionId) -> None:
        """Without blocking anybody, so it is done on its own and not inside a transaction."""
        async with self._db.connection() as connection:
            await connection.execute(
                f"DROP INDEX CONCURRENTLY IF EXISTS knowledge.{vector_index_name(collection_id)}"
            )

    async def delete_collection(self, tenant_id: TenantId, collection_id: CollectionId) -> None:
        async with self._db.transaction(tenant_id) as connection:
            await _collection_row(connection, tenant_id, collection_id)
        await self._drop_vector_index(collection_id)
        async with self._db.transaction(tenant_id) as connection:
            await connection.execute(
                "DELETE FROM knowledge.collections WHERE tenant_id = $1 AND id = $2",
                tenant_id,
                collection_id,
            )

    async def mark_needs_reindex(self, tenant_id: TenantId, collection_id: CollectionId) -> None:
        async with self._db.transaction(tenant_id) as connection:
            await _collection_row(connection, tenant_id, collection_id, lock=True)
            await connection.execute(
                "UPDATE knowledge.collections SET status = 'needs_reindex', updated_at = now() "
                "WHERE tenant_id = $1 AND id = $2 AND status <> 'needs_reindex'",
                tenant_id,
                collection_id,
            )

    async def begin_reindex(
        self, tenant_id: TenantId, collection_id: CollectionId, *, space_id: str, dimensions: int
    ) -> Collection:
        async with self._db.transaction(tenant_id) as connection:
            row = await _collection_row(connection, tenant_id, collection_id)
        already = (
            row["status"] == CollectionStatus.REINDEXING.value
            and row["space_id"] == space_id
            and row["dimensions"] == dimensions
        )
        if not already:
            # The old index is for vectors that are about to be forgotten.
            await self._drop_vector_index(collection_id)
            async with self._db.transaction(tenant_id) as connection:
                await connection.execute(
                    "UPDATE knowledge.chunks SET embedding = NULL "
                    "WHERE tenant_id = $1 AND collection_id = $2",
                    tenant_id,
                    collection_id,
                )
                await connection.execute(
                    "UPDATE knowledge.collections SET space_id = $3, dimensions = $4, "
                    "status = 'reindexing', updated_at = now() WHERE tenant_id = $1 AND id = $2",
                    tenant_id,
                    collection_id,
                    space_id,
                    dimensions,
                )
        return await self.get_collection(tenant_id, collection_id)

    async def finish_reindex(self, tenant_id: TenantId, collection_id: CollectionId) -> Collection:
        async with self._db.transaction(tenant_id) as connection:
            await _collection_row(connection, tenant_id, collection_id, lock=True)
            await connection.execute(
                "UPDATE knowledge.collections SET status = 'active', updated_at = now() "
                "WHERE tenant_id = $1 AND id = $2",
                tenant_id,
                collection_id,
            )
            return await self._read_collection(connection, tenant_id, collection_id)

    async def optimize(self, tenant_id: TenantId, collection_id: CollectionId) -> None:
        collection = await self.get_collection(tenant_id, collection_id)
        form = VectorForm(collection.dimensions)
        if not form.indexable:
            return
        name = vector_index_name(collection_id)
        async with self._db.connection() as connection:
            if await self._index_is_valid(connection, name) is True:
                return
            embedded = await connection.fetchval(
                "SELECT count(*) FROM knowledge.chunks "
                "WHERE collection_id = $1 AND embedding IS NOT NULL",
                collection_id,
            )
            if embedded < self._index_threshold:
                return
            # One builder at a time for one index.
            await connection.execute("SELECT pg_advisory_lock(hashtext($1))", name)
            try:
                state = await self._index_is_valid(connection, name)
                if state is True:
                    return
                if state is False:  # a build that was interrupted leaves an unusable index behind
                    await connection.execute(f"DROP INDEX CONCURRENTLY IF EXISTS knowledge.{name}")
                await connection.execute(f"SET maintenance_work_mem = '{_BUILD_MEMORY}'")
                try:
                    await connection.execute(vector_index_sql(collection_id, collection.dimensions))
                finally:
                    await connection.execute("RESET maintenance_work_mem")
            finally:
                await connection.execute("SELECT pg_advisory_unlock(hashtext($1))", name)

    @staticmethod
    async def _index_is_valid(connection: Connection, name: str) -> bool | None:
        """True if the index exists and can be used, False if it exists and cannot, None if not."""
        found = await connection.fetchrow(
            "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'knowledge' AND c.relname = $1",
            name,
        )
        return None if found is None else bool(found["indisvalid"])

    # ---- documents
    async def add_document(self, tenant_id: TenantId, new: NewDocument) -> Document:
        document_id = DocumentId(uuid4())
        async with self._db.transaction(tenant_id) as connection:
            await _collection_row(connection, tenant_id, new.collection_id)
            try:
                await connection.execute(
                    "INSERT INTO knowledge.documents (id, tenant_id, collection_id, title, source, "
                    "media_type, size_bytes, content_sha256, language, language_declared) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
                    document_id,
                    tenant_id,
                    new.collection_id,
                    new.title,
                    new.source,
                    new.media_type,
                    new.size_bytes,
                    new.content_sha256,
                    new.language,
                    new.language_declared,
                )
            except asyncpg.UniqueViolationError:
                raise ConflictError("the collection already has this document") from None
            await connection.execute(
                "INSERT INTO knowledge.document_data (document_id, tenant_id, data) "
                "VALUES ($1, $2, $3)",
                document_id,
                tenant_id,
                new.data,
            )
            await connection.execute(
                "INSERT INTO knowledge.jobs (id, tenant_id, kind, collection_id, document_id) "
                "VALUES ($1, $2, 'ingest', $3, $4)",
                uuid4(),
                tenant_id,
                new.collection_id,
                document_id,
            )
            return await self._read_document(connection, tenant_id, document_id)

    @staticmethod
    async def _read_document(
        connection: Connection, tenant_id: TenantId, document_id: DocumentId
    ) -> Document:
        row = await connection.fetchrow(
            f"{_DOCUMENT} WHERE d.tenant_id = $1 AND d.id = $2", tenant_id, document_id
        )
        if row is None:
            raise DocumentNotFoundError("the document does not exist")
        return _document(row)

    async def get_document(self, tenant_id: TenantId, document_id: DocumentId) -> Document:
        async with self._db.transaction(tenant_id) as connection:
            return await self._read_document(connection, tenant_id, document_id)

    async def list_documents(
        self, tenant_id: TenantId, collection_id: CollectionId, *, limit: int = 100, offset: int = 0
    ) -> list[Document]:
        async with self._db.transaction(tenant_id) as connection:
            await _collection_row(connection, tenant_id, collection_id)
            rows = await connection.fetch(
                f"{_DOCUMENT} WHERE d.tenant_id = $1 AND d.collection_id = $2 "
                "ORDER BY d.seq DESC LIMIT $3 OFFSET $4",
                tenant_id,
                collection_id,
                limit,
                offset,
            )
        return [_document(row) for row in rows]

    async def document_data(self, tenant_id: TenantId, document_id: DocumentId) -> bytes:
        async with self._db.transaction(tenant_id) as connection:
            data = await connection.fetchval(
                "SELECT data FROM knowledge.document_data "
                "WHERE tenant_id = $1 AND document_id = $2",
                tenant_id,
                document_id,
            )
        if data is None:
            raise DocumentNotFoundError("the document does not exist")
        return bytes(data)

    async def delete_document(self, tenant_id: TenantId, document_id: DocumentId) -> None:
        async with self._db.transaction(tenant_id) as connection:
            deleted = await connection.fetchval(
                "DELETE FROM knowledge.documents WHERE tenant_id = $1 AND id = $2 RETURNING id",
                tenant_id,
                document_id,
            )
        if deleted is None:
            raise DocumentNotFoundError("the document does not exist")

    async def retry_document(self, tenant_id: TenantId, document_id: DocumentId) -> Document:
        async with self._db.transaction(tenant_id) as connection:
            row = await connection.fetchrow(
                "SELECT collection_id, status FROM knowledge.documents "
                "WHERE tenant_id = $1 AND id = $2 FOR UPDATE",
                tenant_id,
                document_id,
            )
            if row is None:
                raise DocumentNotFoundError("the document does not exist")
            if row["status"] != DocumentStatus.FAILED.value:
                raise ConflictError("only a document that failed can be tried again")
            await connection.execute(
                "UPDATE knowledge.documents SET status = 'pending', error_code = NULL, "
                "error_message = NULL, updated_at = now() WHERE id = $1",
                document_id,
            )
            await connection.execute(
                "INSERT INTO knowledge.jobs (id, tenant_id, kind, collection_id, document_id) "
                "VALUES ($1, $2, 'ingest', $3, $4) "
                "ON CONFLICT (document_id) WHERE kind = 'ingest' AND state <> 'failed' "
                "DO NOTHING",
                uuid4(),
                tenant_id,
                row["collection_id"],
                document_id,
            )
            return await self._read_document(connection, tenant_id, document_id)

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
        failed = status is DocumentStatus.FAILED
        async with self._db.transaction(tenant_id) as connection:
            changed = await connection.fetchval(
                "UPDATE knowledge.documents SET status = $3, error_code = $4, error_message = $5, "
                "language = CASE WHEN $6::text IS NOT NULL AND NOT language_declared "
                "THEN $6::text ELSE language END, updated_at = now() "
                "WHERE tenant_id = $1 AND id = $2 RETURNING id",
                tenant_id,
                document_id,
                status.value,
                error_code if failed else None,
                error_message if failed else None,
                language,
            )
        if changed is None:
            raise DocumentNotFoundError("the document does not exist")

    # ---- jobs
    async def enqueue_reindex(self, tenant_id: TenantId, collection_id: CollectionId) -> None:
        async with self._db.transaction(tenant_id) as connection:
            await _collection_row(connection, tenant_id, collection_id)
            await connection.execute(
                "INSERT INTO knowledge.jobs (id, tenant_id, kind, collection_id) "
                "VALUES ($1, $2, 'reindex', $3) "
                "ON CONFLICT (collection_id) WHERE kind = 'reindex' AND state <> 'failed' "
                "DO NOTHING",
                uuid4(),
                tenant_id,
                collection_id,
            )

    async def claim_job(self, worker: str, *, lease_seconds: float) -> Job | None:
        async with self._db.connection() as connection:
            row = await connection.fetchrow(
                "WITH next AS ("
                "SELECT id FROM knowledge.jobs "
                "WHERE (state = 'queued' AND due_at <= now()) "
                "OR (state = 'running' AND lease_until <= now()) "
                "ORDER BY due_at, seq LIMIT 1 FOR UPDATE SKIP LOCKED) "
                "UPDATE knowledge.jobs j SET state = 'running', attempts = j.attempts + 1, "
                "lease_until = now() + make_interval(secs => $2), locked_by = $1 "
                "FROM next WHERE j.id = next.id "
                "RETURNING j.id, j.tenant_id, j.kind, j.collection_id, j.document_id, j.attempts",
                worker,
                lease_seconds,
            )
        if row is None:
            return None
        return Job(
            id=row["id"],
            tenant_id=TenantId(row["tenant_id"]),
            kind=JobKind(row["kind"]),
            collection_id=CollectionId(row["collection_id"]),
            document_id=None if row["document_id"] is None else DocumentId(row["document_id"]),
            attempts=row["attempts"],
        )

    async def extend_job(self, job: Job, *, lease_seconds: float) -> None:
        async with self._db.connection() as connection:
            await connection.execute(
                "UPDATE knowledge.jobs SET lease_until = now() + make_interval(secs => $3) "
                "WHERE id = $1 AND attempts = $2 AND state = 'running'",
                job.id,
                job.attempts,
                lease_seconds,
            )

    async def finish_job(self, job: Job) -> None:
        async with self._db.connection() as connection:
            await connection.execute(
                "DELETE FROM knowledge.jobs WHERE id = $1 AND attempts = $2", job.id, job.attempts
            )

    async def retry_job(self, job: Job, *, delay_seconds: float) -> None:
        async with self._db.connection() as connection:
            await connection.execute(
                "UPDATE knowledge.jobs SET state = 'queued', lease_until = NULL, locked_by = NULL, "
                "due_at = now() + make_interval(secs => $3) WHERE id = $1 AND attempts = $2",
                job.id,
                job.attempts,
                delay_seconds,
            )

    async def fail_job(self, job: Job, error: str) -> None:
        async with self._db.connection() as connection:
            await connection.execute(
                "UPDATE knowledge.jobs SET state = 'failed', lease_until = NULL, locked_by = NULL, "
                "last_error = $3 WHERE id = $1 AND attempts = $2",
                job.id,
                job.attempts,
                error[:_MAX_ERROR_CHARS],
            )

    # ---- pieces
    async def replace_chunks(
        self, tenant_id: TenantId, document_id: DocumentId, chunks: Chunks
    ) -> None:
        async with self._db.transaction(tenant_id) as connection:
            document = await connection.fetchrow(
                "SELECT collection_id, language FROM knowledge.documents "
                "WHERE tenant_id = $1 AND id = $2 FOR UPDATE",
                tenant_id,
                document_id,
            )
            if document is None:
                raise DocumentNotFoundError("the document does not exist")
            configuration = f"knowledge.{fts_config(document['language'])}"

            await connection.execute(
                "DELETE FROM knowledge.parents WHERE tenant_id = $1 AND document_id = $2",
                tenant_id,
                document_id,
            )
            parents = chunks.parents
            await connection.execute(
                "INSERT INTO knowledge.parents (id, tenant_id, document_id, ordinal, text, "
                "headings, page) "
                "SELECT t.id, $2::uuid, $3::uuid, t.ordinal, t.text, t.headings::jsonb, t.page "
                "FROM unnest($1::uuid[], $4::int[], $5::text[], $6::text[], $7::int[]) "
                "AS t(id, ordinal, text, headings, page)",
                [parent_id(document_id, p.ordinal) for p in parents],
                tenant_id,
                document_id,
                [p.ordinal for p in parents],
                [p.text for p in parents],
                [json.dumps(p.headings) for p in parents],
                [p.page for p in parents],
            )
            children = chunks.children
            await connection.execute(
                "INSERT INTO knowledge.chunks (id, tenant_id, collection_id, document_id, "
                "parent_id, ordinal, text, headings, page, fts_config, tsv) "
                "SELECT t.id, $2::uuid, $3::uuid, $4::uuid, t.parent_id, t.ordinal, t.text, "
                "t.headings::jsonb, "
                "t.page, $5::text, "
                "setweight(to_tsvector($5::text::regconfig, t.crumb), 'B') || "
                "setweight(to_tsvector($5::text::regconfig, t.text), 'A') "
                "FROM unnest($1::uuid[], $6::uuid[], $7::int[], $8::text[], $9::text[], "
                "$10::text[], $11::int[]) "
                "AS t(id, parent_id, ordinal, text, headings, crumb, page)",
                [chunk_id(document_id, c.ordinal) for c in children],
                tenant_id,
                document["collection_id"],
                document_id,
                configuration,
                [parent_id(document_id, c.parent) for c in children],
                [c.ordinal for c in children],
                [c.text for c in children],
                [json.dumps(c.headings) for c in children],
                [breadcrumb(c.headings) for c in children],
                [c.page for c in children],
            )
            await connection.execute(
                "UPDATE knowledge.documents SET updated_at = now() WHERE id = $1", document_id
            )

    async def pending_embeddings(
        self,
        tenant_id: TenantId,
        collection_id: CollectionId,
        *,
        limit: int,
        document_id: DocumentId | None = None,
    ) -> list[PendingChunk]:
        async with self._db.transaction(tenant_id) as connection:
            await _collection_row(connection, tenant_id, collection_id)
            rows = await connection.fetch(
                "SELECT c.id, c.document_id, c.headings, c.text FROM knowledge.chunks c "
                "JOIN knowledge.documents d ON d.id = c.document_id "
                "WHERE c.tenant_id = $1 AND c.collection_id = $2 AND c.embedding IS NULL "
                "AND ($4::uuid IS NULL OR c.document_id = $4::uuid) "
                "ORDER BY d.seq, c.ordinal LIMIT $3",
                tenant_id,
                collection_id,
                limit,
                document_id,
            )
        return [
            PendingChunk(
                ChunkId(row["id"]),
                DocumentId(row["document_id"]),
                tuple(json.loads(row["headings"])),
                row["text"],
            )
            for row in rows
        ]

    async def store_embeddings(
        self,
        tenant_id: TenantId,
        collection_id: CollectionId,
        items: Sequence[tuple[ChunkId, Sequence[float]]],
    ) -> None:
        async with self._db.transaction(tenant_id) as connection:
            collection = await _collection_row(connection, tenant_id, collection_id)
            for _, vector in items:
                if len(vector) != collection["dimensions"]:
                    raise ValueError(
                        f"a vector of {len(vector)} numbers does not fit a collection "
                        f"of {collection['dimensions']}"
                    )
            if not items:
                return
            await connection.execute(
                "UPDATE knowledge.chunks c SET embedding = t.embedding::vector "
                "FROM unnest($3::uuid[], $4::text[]) AS t(id, embedding) "
                "WHERE c.id = t.id AND c.tenant_id = $1 AND c.collection_id = $2",
                tenant_id,
                collection_id,
                [identity for identity, _ in items],
                [vector_text(vector) for _, vector in items],
            )

    # ---- search
    async def search_vector(
        self,
        tenant_id: TenantId,
        collection_id: CollectionId,
        vector: Sequence[float],
        *,
        limit: int,
    ) -> list[Hit]:
        async with self._db.transaction(tenant_id) as connection:
            collection = await _collection_row(connection, tenant_id, collection_id)
            dimensions: int = collection["dimensions"]
            if len(vector) != dimensions:
                raise ValueError("the query vector does not fit the collection")
            # How many candidates the index looks at: more than are asked for.
            effort = min(max(_MIN_EF_SEARCH, limit * 2), _MAX_EF_SEARCH)
            await connection.execute("SELECT set_config('hnsw.ef_search', $1, true)", str(effort))
            rows = await connection.fetch(
                vector_search_sql(collection_id, dimensions), tenant_id, vector_text(vector), limit
            )
        return [_hit(row) for row in rows]

    async def search_text(
        self,
        tenant_id: TenantId,
        collection_ids: Sequence[CollectionId],
        query: str,
        *,
        limit: int,
    ) -> list[Hit]:
        wanted = list(dict.fromkeys(collection_ids))
        if not wanted:
            return []
        async with self._db.transaction(tenant_id) as connection:
            known = await connection.fetchval(
                "SELECT count(*) FROM knowledge.collections "
                "WHERE tenant_id = $1 AND id = ANY($2::uuid[])",
                tenant_id,
                wanted,
            )
            if known != len(wanted):
                raise CollectionNotFoundError("the collection does not exist")
            languages = await connection.fetch(
                "SELECT DISTINCT language FROM knowledge.documents "
                "WHERE collection_id = ANY($1::uuid[])",
                wanted,
            )
            configurations = sorted({fts_config(row["language"]) for row in languages})
            found: list[list[Hit]] = []
            for configuration in configurations:
                rows = await connection.fetch(
                    TEXT_SEARCH_SQL,
                    tenant_id,
                    wanted,
                    query,
                    f"knowledge.{configuration}",
                    f"knowledge.{configuration}",
                    limit,
                )
                found.append([_hit(row) for row in rows])
        return _merged(found, limit)


def _merged(lists: list[list[Hit]], limit: int) -> list[Hit]:
    """One ranking from the hits of each language: each is ranked in its own terms."""
    if len(lists) <= 1:
        return lists[0] if lists else []
    by_id = {hit.chunk_id: hit for hits in lists for hit in hits}
    fused = reciprocal_rank_fusion([[hit.chunk_id for hit in hits] for hits in lists])
    return [replace(by_id[identity], score=score) for identity, score in fused[:limit]]
