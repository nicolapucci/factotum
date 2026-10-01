"""Putting the service together from its settings: the database, the gateway, the knowledge service.

Everything that needs a running event loop (the connection pool, the clients, the work in the
background) is made when the application starts and closed when it stops, in its lifespan. The
clients to the gateway and to the knowledge service live as long as the service does, so the
connections to them are kept between turns.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from fastapi import FastAPI

from factotum.common.devauth import DEV_TENANT_ID, DEV_USER_ID
from factotum.common.hubauth import authenticator_for, principal_delegation_for
from factotum.conversation.actions import Actions, ActionsClient
from factotum.conversation.api import create_app
from factotum.conversation.context import PromptBuilder
from factotum.conversation.documents import KnowledgeClient
from factotum.conversation.gate import ReferenceGate
from factotum.conversation.history import History, Keeper
from factotum.conversation.service import ConversationService
from factotum.conversation.settings import ConversationSettings
from factotum.conversation.store.postgres import PostgresStore, prepare
from factotum.conversation.turn import TurnEngine
from factotum.db.pool import Database
from factotum.model_client import DevDelegation, GatewayChat, GatewayEmbedder

_KNOWLEDGE_TIMEOUT = httpx.Timeout(10.0, connect=3.0)


def build_app(settings: ConversationSettings) -> FastAPI:
    """The service as a web application.

    Authentication is the hub's once a token secret is configured (``FACTOTUM_HUB_TOKEN_SECRET``);
    otherwise the development authenticator, which refuses to exist outside development and test,
    so without one in production this raises and the service does not start.
    """
    authenticator = authenticator_for(settings, tenant_id=DEV_TENANT_ID, user_id=DEV_USER_ID)
    delegation = DevDelegation(settings)
    principals = principal_delegation_for(settings)
    holder: dict[str, Database] = {}
    model_timeout = httpx.Timeout(
        connect=5.0, read=settings.conversation_answer_timeout_s, write=30.0, pool=5.0
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = await Database.connect(settings)
        holder["database"] = database
        try:
            await prepare(database)
            store = PostgresStore(database)
            async with (
                AsyncExitStack() as optional,
                httpx.AsyncClient(
                    base_url=str(settings.conversation_gateway_url), timeout=model_timeout
                ) as gateway,
                httpx.AsyncClient(
                    base_url=str(settings.conversation_knowledge_url), timeout=_KNOWLEDGE_TIMEOUT
                ) as knowledge,
            ):
                actions: Actions | None = None
                if settings.conversation_actions_url is not None:
                    actions = ActionsClient(
                        await optional.enter_async_context(
                            httpx.AsyncClient(
                                base_url=str(settings.conversation_actions_url),
                                timeout=httpx.Timeout(
                                    connect=3.0,
                                    read=settings.conversation_actions_timeout_s + 5.0,
                                    write=10.0,
                                    pool=5.0,
                                ),
                            )
                        ),
                        principals,
                    )
                chat = GatewayChat(gateway, delegation)
                embedder = GatewayEmbedder(
                    gateway, delegation, batch=settings.conversation_embed_batch
                )
                history = History(
                    store,
                    embedder,
                    zone=settings.zone,
                    min_similarity=settings.conversation_history_min_similarity,
                )
                engine = TurnEngine(
                    store,
                    chat,
                    KnowledgeClient(knowledge, delegation),
                    history,
                    ReferenceGate(chat, timeout_s=settings.conversation_gate_timeout_s),
                    PromptBuilder(zone=settings.zone),
                    actions=actions,
                    settings=settings.turn,
                )
                app.state.service = ConversationService(store, engine, history)
                keeper = Keeper(
                    history,
                    store,
                    interval_s=settings.conversation_keeper_seconds,
                    batch=settings.conversation_embed_batch,
                    retention=settings.retention,
                    shared_keep=settings.shared_keep,
                )
                stop = asyncio.Event()
                working = asyncio.create_task(keeper.run(stop))
                try:
                    yield
                finally:
                    stop.set()
                    await working
        finally:
            await database.close()

    async def ready() -> bool:
        database = holder.get("database")
        return database is not None and await database.ping()

    return create_app(
        authenticator, docs=not settings.is_production, lifespan=lifespan, ready=ready
    )
