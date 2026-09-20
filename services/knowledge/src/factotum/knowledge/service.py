"""What the knowledge service does for a caller: the operations behind its HTTP interface.

Every operation starts from *who is asking* (a ``Principal``) and lets the policy say what they may
see and change (``policy``); the store never decides that. What is refused is refused in a way that
tells nothing about what exists: a collection somebody may not see is not found.
"""

import hashlib
import re

from factotum.contracts.errors import DocumentNotFoundError, DocumentTooLargeError
from factotum.contracts.identity import Principal
from factotum.contracts.ids import CollectionId, DocumentId
from factotum.contracts.knowledge import (
    Collection,
    CollectionCreate,
    CollectionUpdate,
    Document,
    DocumentText,
    SearchRequest,
    SearchResult,
)
from factotum.knowledge.document import Limits
from factotum.knowledge.embedder import PROBE, Embedder
from factotum.knowledge.language import normalize_language
from factotum.knowledge.loaders import sniff
from factotum.knowledge.policy import can_read, manageable, owner_for, readable
from factotum.knowledge.retrieval import Retriever
from factotum.knowledge.store import KnowledgeStore, NewDocument

_MAX_NAME = 200
_MAX_TITLE = 300
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _file_name(raw: str | None) -> str | None:
    """A file name and nothing else: no directories, no control characters, not too long. It is
    shown, never used to reach a file."""
    if not raw:
        return None
    name = _CONTROL.sub("", raw.replace("\\", "/").rsplit("/", 1)[-1]).strip()
    return name[:_MAX_NAME] or None


def _title(name: str | None) -> str:
    if not name:
        return "Untitled"
    stem = name.rsplit(".", 1)[0] if "." in name.lstrip(".") else name
    return (stem.strip() or name)[:_MAX_TITLE]


class KnowledgeService:
    def __init__(
        self,
        store: KnowledgeStore,
        embedder: Embedder,
        *,
        limits: Limits | None = None,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._limits = limits or Limits()
        self._retriever = Retriever(store, embedder)

    @property
    def max_upload_bytes(self) -> int:
        """The largest file that is kept."""
        return self._limits.max_bytes

    # ---- collections
    async def create_collection(
        self, principal: Principal, request: CollectionCreate
    ) -> Collection:
        owner = owner_for(principal, request.owner)
        # The collection lives in the space of the owner's embedding model, which asking it tells.
        space = await self._embedder.embed(owner, [PROBE])
        return await self._store.create_collection(
            principal.tenant_id,
            owner,
            name=request.name,
            description=request.description,
            space_id=space.space_id,
            dimensions=space.dimensions,
        )

    async def list_collections(self, principal: Principal) -> list[Collection]:
        everything = await self._store.list_collections(principal.tenant_id)
        return [collection for collection in everything if can_read(principal, collection)]

    async def get_collection(self, principal: Principal, collection_id: CollectionId) -> Collection:
        return readable(
            principal, await self._store.get_collection(principal.tenant_id, collection_id)
        )

    async def update_collection(
        self, principal: Principal, collection_id: CollectionId, update: CollectionUpdate
    ) -> Collection:
        manageable(principal, await self._store.get_collection(principal.tenant_id, collection_id))
        return await self._store.update_collection(principal.tenant_id, collection_id, update)

    async def delete_collection(self, principal: Principal, collection_id: CollectionId) -> None:
        manageable(principal, await self._store.get_collection(principal.tenant_id, collection_id))
        await self._store.delete_collection(principal.tenant_id, collection_id)

    async def reindex_collection(self, principal: Principal, collection_id: CollectionId) -> None:
        manageable(principal, await self._store.get_collection(principal.tenant_id, collection_id))
        await self._store.enqueue_reindex(principal.tenant_id, collection_id)

    # ---- documents
    async def add_file(
        self,
        principal: Principal,
        collection_id: CollectionId,
        *,
        content: bytes,
        filename: str | None = None,
        declared_type: str | None = None,
        title: str | None = None,
        language: str | None = None,
    ) -> Document:
        """Keep an uploaded file and queue it to be read. What it is comes from what is in it, not
        from what it is called."""
        manageable(principal, await self._store.get_collection(principal.tenant_id, collection_id))
        if len(content) > self._limits.max_bytes:
            raise DocumentTooLargeError(
                f"the file is larger than {self._limits.max_bytes // (1024 * 1024)} MiB"
            )
        media_type = sniff.detect_media_type(content, filename=filename, declared=declared_type)
        name = _file_name(filename)
        return await self._add(
            principal, collection_id, content, media_type, name, title or _title(name), language
        )

    async def add_text(
        self, principal: Principal, collection_id: CollectionId, document: DocumentText
    ) -> Document:
        """Keep a document that was sent as text."""
        manageable(principal, await self._store.get_collection(principal.tenant_id, collection_id))
        content = document.text.encode()
        if len(content) > self._limits.max_bytes:
            raise DocumentTooLargeError(
                f"the text is larger than {self._limits.max_bytes // (1024 * 1024)} MiB"
            )
        return await self._add(
            principal,
            collection_id,
            content,
            sniff.PLAIN,
            None,
            document.title[:_MAX_TITLE],
            document.language,
        )

    async def _add(
        self,
        principal: Principal,
        collection_id: CollectionId,
        content: bytes,
        media_type: str,
        source: str | None,
        title: str,
        language: str | None,
    ) -> Document:
        declared = normalize_language(language) if language else None
        if language and declared is None:
            raise ValueError("the language is not a language code")
        return await self._store.add_document(
            principal.tenant_id,
            NewDocument(
                collection_id=collection_id,
                title=title,
                source=source,
                media_type=media_type,
                size_bytes=len(content),
                content_sha256=hashlib.sha256(content).hexdigest(),
                language=declared,
                language_declared=declared is not None,
                data=content,
            ),
        )

    async def list_documents(
        self,
        principal: Principal,
        collection_id: CollectionId,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Document]:
        readable(principal, await self._store.get_collection(principal.tenant_id, collection_id))
        return await self._store.list_documents(
            principal.tenant_id, collection_id, limit=limit, offset=offset
        )

    async def _document_in_reach(
        self, principal: Principal, document_id: DocumentId, *, change: bool
    ) -> Document:
        """A document of a collection the principal may see (and, to change it, may change)."""
        document = await self._store.get_document(principal.tenant_id, document_id)
        collection = await self._store.get_collection(principal.tenant_id, document.collection_id)
        if not can_read(principal, collection):
            raise DocumentNotFoundError("the document does not exist")
        if change:
            manageable(principal, collection)
        return document

    async def get_document(self, principal: Principal, document_id: DocumentId) -> Document:
        return await self._document_in_reach(principal, document_id, change=False)

    async def delete_document(self, principal: Principal, document_id: DocumentId) -> None:
        await self._document_in_reach(principal, document_id, change=True)
        await self._store.delete_document(principal.tenant_id, document_id)

    async def retry_document(self, principal: Principal, document_id: DocumentId) -> Document:
        await self._document_in_reach(principal, document_id, change=True)
        return await self._store.retry_document(principal.tenant_id, document_id)

    # ---- searching
    async def search(self, principal: Principal, request: SearchRequest) -> SearchResult:
        return await self._retriever.search(principal, request)
