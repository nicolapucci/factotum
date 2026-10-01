"""Putting the service together from its settings: the database, the gateway, the workers.

Everything that needs a running event loop (the connection pool, the workers) is made when the
application starts and closed when it stops, in its lifespan.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from factotum.common.devauth import DEV_TENANT_ID, DEV_USER_ID
from factotum.common.hubauth import authenticator_for
from factotum.contracts.identity import SINGLE_TENANT_ID
from factotum.db.pool import Database
from factotum.knowledge.api import create_app
from factotum.knowledge.ingestion import Ingestor
from factotum.knowledge.service import KnowledgeService
from factotum.knowledge.settings import KnowledgeSettings
from factotum.knowledge.store.postgres import PostgresStore, prepare
from factotum.knowledge.worker import Worker
from factotum.model_client import GatewayEmbedder, delegation_for

_GATEWAY_TIMEOUT = httpx.Timeout(120.0, connect=5.0)
"""Embedding a batch with a model on the machine takes seconds; reaching the gateway does not."""

_STOP_GRACE_S = 30.0
"""How long a worker gets to finish the document it is reading when the service stops."""


def build_app(settings: KnowledgeSettings) -> FastAPI:
    """The service as a web application.

    Authentication is the hub's once a token secret is configured (``FACTOTUM_HUB_TOKEN_SECRET``);
    otherwise the development authenticator, which refuses to exist outside development and test,
    so without one in production this raises and the service does not start. Proving an owner to
    the model gateway follows the same switch.
    """
    authenticator = authenticator_for(settings, tenant_id=DEV_TENANT_ID, user_id=DEV_USER_ID)
    delegation = delegation_for(settings, SINGLE_TENANT_ID)
    holder: dict[str, Database] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = await Database.connect(settings)
        holder["database"] = database
        try:
            await prepare(database)
            store = PostgresStore(database, index_threshold=settings.knowledge_index_threshold)
            async with httpx.AsyncClient(
                base_url=str(settings.knowledge_gateway_url), timeout=_GATEWAY_TIMEOUT
            ) as client:
                embedder = GatewayEmbedder(client, delegation, batch=settings.knowledge_embed_batch)
                app.state.service = KnowledgeService(store, embedder, limits=settings.limits)
                stop = asyncio.Event()
                working = (
                    asyncio.create_task(
                        Worker(
                            store,
                            Ingestor(store, embedder, settings=settings.ingestion),
                            concurrency=settings.knowledge_workers,
                        ).run(stop)
                    )
                    if settings.knowledge_workers > 0
                    else None
                )
                try:
                    yield
                finally:
                    stop.set()
                    if working is not None:
                        try:
                            await asyncio.wait_for(working, _STOP_GRACE_S)
                        except TimeoutError:
                            working.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await working
        finally:
            await database.close()

    async def ready() -> bool:
        database = holder.get("database")
        return database is not None and await database.ping()

    return create_app(
        authenticator,
        docs=not settings.is_production,
        max_upload_bytes=settings.limits.max_bytes,
        lifespan=lifespan,
        ready=ready,
    )
