"""Reading a document into a collection: parse it, cut it, embed the pieces.

A job is one of two things. **Ingest** takes a document that was uploaded and makes it searchable.
**Reindex** embeds a whole collection again, for when the embedding model behind its owner's role is
no longer the one that made its vectors. Both are safe to run twice: a job whose worker died is
given to another, and picks up where the first stopped (the pieces already embedded are not
embedded again, and the file is not read again).

What can go wrong is of two kinds, and told apart. A document that cannot be read, is too big, or
whose pieces the embedding model will not take, is *failed*, with a reason the uploader can act on,
and the job is done: asking again would give the same answer. A gateway that is down or a model
that is busy is *retried*, later and later, and only after several attempts is the document failed.
"""

import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from factotum.contracts.errors import (
    CollectionNotFoundError,
    ContextTooLongError,
    DocumentNotFoundError,
    EmbeddingNotConfiguredError,
    FactotumError,
    GatewayUnavailableError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    RateLimitedError,
    UnsupportedDocumentError,
)
from factotum.contracts.ids import CollectionId, DocumentId, TenantId
from factotum.contracts.knowledge import Collection, CollectionStatus, DocumentStatus
from factotum.knowledge.chunker import ChunkingConfig, Chunks, chunk, embedding_input
from factotum.knowledge.document import Limits, ParsedDocument
from factotum.knowledge.embedder import PROBE, Embedder
from factotum.knowledge.language import detect_language
from factotum.knowledge.loaders.isolation import parse_isolated
from factotum.knowledge.store import Job, JobKind, KnowledgeStore

logger = logging.getLogger("factotum.knowledge")

Parser = Callable[[bytes, str, Limits], Awaitable[ParsedDocument]]

_RETRYABLE = (
    GatewayUnavailableError,
    ProviderUnavailableError,
    ProviderTimeoutError,
    RateLimitedError,
)
_MAX_BACKOFF_S = 900.0


@dataclass(frozen=True, slots=True)
class IngestionSettings:
    limits: Limits = field(default_factory=Limits)
    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)
    batch: int = 32
    """Pieces embedded per request to the gateway."""

    lease_seconds: float = 600.0
    """How long a job is a worker's, renewed after each batch."""

    max_attempts: int = 5
    retry_base_seconds: float = 30.0
    parse_timeout_s: float = 300.0


def _backoff(settings: IngestionSettings, attempts: int, error: Exception) -> float:
    delay: float = min(settings.retry_base_seconds * 2 ** (attempts - 1), _MAX_BACKOFF_S)
    if isinstance(error, RateLimitedError) and error.retry_after_s is not None:
        delay = max(delay, error.retry_after_s)
    return delay


def _reason(error: Exception) -> tuple[str, str]:
    """A code and a message for the person whose document it was."""
    if isinstance(error, EmbeddingNotConfiguredError):
        return (
            error.code,
            "no embedding model is set up for the owner of the collection: set one up, "
            "then try again",
        )
    if isinstance(error, ContextTooLongError):
        return (
            error.code,
            "the embedding model cannot take even the smallest pieces the document can be cut into",
        )
    if isinstance(error, _RETRYABLE):
        return "embedding_unavailable", "the embedding model could not be reached: try again later"
    if isinstance(error, FactotumError):
        return error.code, error.message
    return "internal_error", "the document could not be processed"


class Ingestor:
    def __init__(
        self,
        store: KnowledgeStore,
        embedder: Embedder,
        *,
        settings: IngestionSettings | None = None,
        parser: Parser | None = None,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._settings = settings or IngestionSettings()
        self._parser = parser or self._parse_isolated

    async def _parse_isolated(self, data: bytes, media_type: str, limits: Limits) -> ParsedDocument:
        return await parse_isolated(
            data, media_type, limits, timeout_s=self._settings.parse_timeout_s
        )

    # ---- a job, whatever happens to it
    async def run(self, job: Job) -> None:
        """Do a job that was claimed, and see to what becomes of it. If the worker is stopped
        meanwhile the job is left as it is: its lease runs out and it goes to another worker."""
        try:
            if job.kind is JobKind.INGEST:
                await self._ingest(job)
            else:
                await self._reindex(job)
            await self._store.finish_job(job)
        except (DocumentNotFoundError, CollectionNotFoundError):
            # Deleted while it was being read: nothing is left to do, and nobody to tell.
            await self._store.finish_job(job)
        except _RETRYABLE as error:
            await self._later(job, error)
        except FactotumError as error:
            await self._give_up(job, error, retry=False)
        except Exception as error:
            logger.exception("a document job failed unexpectedly", extra=self._extra(job))
            await self._later(job, error)

    @staticmethod
    def _extra(job: Job) -> dict[str, object]:
        return {
            "job_id": str(job.id),
            "kind": job.kind.value,
            "collection_id": str(job.collection_id),
            "attempts": job.attempts,
        }

    async def _later(self, job: Job, error: Exception) -> None:
        if job.attempts >= self._settings.max_attempts:
            await self._give_up(job, error, retry=True)
            return
        delay = _backoff(self._settings, job.attempts, error)
        logger.warning(
            "a document job will be tried again",
            extra={**self._extra(job), "error": type(error).__name__, "delay_s": delay},
        )
        await self._store.retry_job(job, delay_seconds=delay)

    async def _give_up(self, job: Job, error: Exception, *, retry: bool) -> None:
        """The job cannot succeed: say why on the document, and drop what was made of it."""
        code, message = _reason(error)
        logger.warning(
            "a document job failed", extra={**self._extra(job), "error": code, "retried": retry}
        )
        if job.document_id is not None:
            try:
                await self._store.set_document_state(
                    job.tenant_id,
                    job.document_id,
                    DocumentStatus.FAILED,
                    error_code=code,
                    error_message=message,
                )
                # A document that failed contributes nothing to a search.
                await self._store.replace_chunks(job.tenant_id, job.document_id, Chunks((), ()))
            except (DocumentNotFoundError, CollectionNotFoundError):
                pass
        if job.kind is JobKind.REINDEX:
            # A re-embedding that had begun and was not finished leaves a collection that is neither
            # the old nor the new, and nobody working on it. One that never began changed nothing.
            with contextlib.suppress(CollectionNotFoundError):
                collection = await self._store.get_collection(job.tenant_id, job.collection_id)
                if collection.status is CollectionStatus.REINDEXING:
                    await self._store.mark_needs_reindex(job.tenant_id, job.collection_id)
        if retry:
            await self._store.fail_job(job, code)
        else:
            await self._store.finish_job(job)

    # ---- ingest
    async def _ingest(self, job: Job) -> None:
        tenant, document_id = job.tenant_id, job.document_id
        if document_id is None:  # pragma: no cover - the store makes ingest jobs with a document
            raise UnsupportedDocumentError("the job has no document")
        document = await self._store.get_document(tenant, document_id)
        await self._store.set_document_state(tenant, document_id, DocumentStatus.PROCESSING)

        parsed: ParsedDocument | None = None
        config = self._settings.chunking
        if document.chunk_count == 0:
            parsed = await self._read(
                tenant, document_id, document.media_type, document.language_declared
            )
            await self._cut(tenant, document_id, parsed, config)

        while True:
            try:
                await self._embed_pending(job, document.collection_id, document_id)
                break
            except ContextTooLongError:
                if not config.can_shrink:
                    raise
                config = config.shrunk()
                if parsed is None:
                    parsed = await self._read(
                        tenant, document_id, document.media_type, document.language_declared
                    )
                await self._cut(tenant, document_id, parsed, config)

        await self._store.set_document_state(tenant, document_id, DocumentStatus.READY)
        await self._optimize(tenant, document.collection_id)

    async def _optimize(self, tenant: TenantId, collection_id: CollectionId) -> None:
        """Make a grown collection fast to search. It is an optimization: a search is right without
        it, so failing to build the index is never a reason to fail the document that was just
        made searchable. The next document or re-index tries again."""
        try:
            await self._store.optimize(tenant, collection_id)
        except CollectionNotFoundError:
            raise
        except Exception:
            logger.exception(
                "the search index of a collection could not be built",
                extra={"collection_id": str(collection_id)},
            )

    async def _read(
        self, tenant: TenantId, document_id: DocumentId, media_type: str, declared: bool
    ) -> ParsedDocument:
        data = await self._store.document_data(tenant, document_id)
        parsed = await self._parser(data, media_type, self._settings.limits)
        language = None if declared else detect_language(list(parsed.blocks))
        await self._store.set_document_state(
            tenant, document_id, DocumentStatus.PROCESSING, language=language
        )
        return parsed

    async def _cut(
        self,
        tenant: TenantId,
        document_id: DocumentId,
        parsed: ParsedDocument,
        config: ChunkingConfig,
    ) -> None:
        chunks = chunk(parsed.blocks, config)
        if not chunks.children:
            raise UnsupportedDocumentError("there is no text in the document to search")
        await self._store.replace_chunks(tenant, document_id, chunks)

    # ---- embedding
    async def _embed_pending(
        self, job: Job, collection_id: CollectionId, document_id: DocumentId | None
    ) -> bool:
        """Embed what has no vector yet, a batch at a time. False if the embedding model turned out
        not to be the one the collection was made with: its pieces stay as text until it is
        reindexed."""
        tenant = job.tenant_id
        while True:
            pending = await self._store.pending_embeddings(
                tenant, collection_id, limit=self._settings.batch, document_id=document_id
            )
            if not pending:
                return True
            collection = await self._store.get_collection(tenant, collection_id)
            embedded = await self._embedder.embed(
                collection.owner, [embedding_input(p.headings, p.text) for p in pending]
            )
            if embedded.space_id != collection.space_id:
                await self._store.mark_needs_reindex(tenant, collection_id)
                return False
            await self._store.store_embeddings(
                tenant,
                collection_id,
                [(p.chunk_id, vector) for p, vector in zip(pending, embedded.vectors, strict=True)],
            )
            await self._store.extend_job(job, lease_seconds=self._settings.lease_seconds)

    # ---- reindex
    async def _reindex(self, job: Job) -> None:
        tenant, collection_id = job.tenant_id, job.collection_id
        collection: Collection = await self._store.get_collection(tenant, collection_id)
        current = await self._embedder.embed(collection.owner, [PROBE])
        await self._store.begin_reindex(
            tenant, collection_id, space_id=current.space_id, dimensions=current.dimensions
        )
        if await self._embed_pending(job, collection_id, None):
            await self._store.finish_reindex(tenant, collection_id)
            await self._optimize(tenant, collection_id)
