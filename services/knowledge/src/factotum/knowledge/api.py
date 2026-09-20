"""The HTTP interface of the knowledge service.

Everything is under ``/v1`` and needs an authenticated caller: what a caller sees and may change
follows from who they are (``policy``), never from what they ask. Adding a document answers at
once (``202``) with the document waiting to be read; reading it happens in the workers, and its
``status`` and ``progress`` say how far it is. A search answers with passages that are somebody
else's words: each says so (``untrusted``).
"""

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, FastAPI, Form, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from starlette.types import Lifespan

from factotum.contracts.ids import CollectionId, DocumentId
from factotum.contracts.knowledge import (
    Collection,
    CollectionCreate,
    CollectionList,
    CollectionUpdate,
    Document,
    DocumentList,
    DocumentText,
    SearchRequest,
    SearchResult,
)
from factotum.knowledge.document import Limits
from factotum.knowledge.service import KnowledgeService
from factotum.web import Authenticator, Caller, create_base_app

_MULTIPART_OVERHEAD = 1024 * 1024
"""What a multipart body weighs beyond the file it carries."""

_LANGUAGE = r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*$"


def _service(request: Request) -> KnowledgeService:
    service: KnowledgeService = request.app.state.service
    return service


Service = Annotated[KnowledgeService, Depends(_service)]


def create_app(
    authenticator: Authenticator,
    *,
    service: KnowledgeService | None = None,
    docs: bool = False,
    max_upload_bytes: int = Limits().max_bytes,
    lifespan: Lifespan[FastAPI] | None = None,
    ready: Callable[[], Awaitable[bool]] | None = None,
) -> FastAPI:
    """The service as a web application. It is given the operations to serve (or, when it is
    put together while it starts, ``lifespan`` sets ``app.state.service``) and, for ``/readyz``, a
    way to tell whether what it depends on answers."""
    app = create_base_app(
        "Factotum knowledge",
        authenticator,
        docs=docs,
        max_body_bytes=max_upload_bytes + _MULTIPART_OVERHEAD,
        lifespan=lifespan,
    )
    app.state.service = service
    app.state.ready = ready

    @app.get("/healthz", summary="Liveness")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", summary="Readiness", response_model=None)
    async def readyz(request: Request) -> Response:
        check: Callable[[], Awaitable[bool]] | None = request.app.state.ready
        answering = True if check is None else await check()
        return JSONResponse(
            {"status": "ok" if answering else "unavailable"}, status_code=200 if answering else 503
        )

    # ---- collections
    @app.post("/v1/collections", status_code=201, summary="Create a collection")
    async def create_collection(
        body: CollectionCreate, principal: Caller, service: Service
    ) -> Collection:
        return await service.create_collection(principal, body)

    @app.get("/v1/collections", summary="The collections the caller may see")
    async def list_collections(principal: Caller, service: Service) -> CollectionList:
        return CollectionList(collections=tuple(await service.list_collections(principal)))

    @app.get("/v1/collections/{collection_id}", summary="One collection")
    async def get_collection(
        collection_id: CollectionId, principal: Caller, service: Service
    ) -> Collection:
        return await service.get_collection(principal, collection_id)

    @app.patch("/v1/collections/{collection_id}", summary="Rename or describe a collection")
    async def update_collection(
        collection_id: CollectionId, body: CollectionUpdate, principal: Caller, service: Service
    ) -> Collection:
        return await service.update_collection(principal, collection_id, body)

    @app.delete(
        "/v1/collections/{collection_id}",
        status_code=204,
        response_class=Response,
        summary="Delete a collection with all that is in it",
    )
    async def delete_collection(
        collection_id: CollectionId, principal: Caller, service: Service
    ) -> Response:
        await service.delete_collection(principal, collection_id)
        return Response(status_code=204)

    @app.post(
        "/v1/collections/{collection_id}/reindex",
        status_code=202,
        response_class=Response,
        summary="Embed the collection again with the model its owner has now",
    )
    async def reindex_collection(
        collection_id: CollectionId, principal: Caller, service: Service
    ) -> Response:
        await service.reindex_collection(principal, collection_id)
        return Response(status_code=202)

    # ---- documents
    @app.post(
        "/v1/collections/{collection_id}/documents",
        status_code=202,
        summary="Upload a file",
    )
    async def upload_document(
        collection_id: CollectionId,
        file: UploadFile,
        principal: Caller,
        service: Service,
        title: Annotated[str | None, Form(max_length=300)] = None,
        language: Annotated[str | None, Form(pattern=_LANGUAGE)] = None,
    ) -> Document:
        # One byte more than may be kept, to tell a file that fits from one that does not.
        content = await file.read(service.max_upload_bytes + 1)
        return await service.add_file(
            principal,
            collection_id,
            content=content,
            filename=file.filename,
            declared_type=file.content_type,
            title=title or None,
            language=language or None,
        )

    @app.post(
        "/v1/collections/{collection_id}/documents/text",
        status_code=202,
        summary="Add a document sent as text",
    )
    async def add_text(
        collection_id: CollectionId, body: DocumentText, principal: Caller, service: Service
    ) -> Document:
        return await service.add_text(principal, collection_id, body)

    @app.get(
        "/v1/collections/{collection_id}/documents",
        summary="The documents of a collection, newest first",
    )
    async def list_documents(
        collection_id: CollectionId,
        principal: Caller,
        service: Service,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> DocumentList:
        documents = await service.list_documents(
            principal, collection_id, limit=limit, offset=offset
        )
        return DocumentList(documents=tuple(documents))

    @app.get("/v1/documents/{document_id}", summary="One document, and how far it is")
    async def get_document(
        document_id: DocumentId, principal: Caller, service: Service
    ) -> Document:
        return await service.get_document(principal, document_id)

    @app.delete(
        "/v1/documents/{document_id}",
        status_code=204,
        response_class=Response,
        summary="Delete a document",
    )
    async def delete_document(
        document_id: DocumentId, principal: Caller, service: Service
    ) -> Response:
        await service.delete_document(principal, document_id)
        return Response(status_code=204)

    @app.post(
        "/v1/documents/{document_id}/retry",
        status_code=202,
        summary="Try again a document that failed",
    )
    async def retry_document(
        document_id: DocumentId, principal: Caller, service: Service
    ) -> Document:
        return await service.retry_document(principal, document_id)

    # ---- searching
    @app.post("/v1/search", summary="Search by meaning and by words")
    async def search(body: SearchRequest, principal: Caller, service: Service) -> SearchResult:
        return await service.search(principal, body)

    return app
