"""Putting the hub together from its settings: the database, the token secret, and the tenant and
its administrator, created the first time it starts.

Everything that needs a running event loop (the connection pool) is made when the application starts
and closed when it stops, in its lifespan.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from factotum.common.hubauth import HubAuthenticator
from factotum.contracts.errors import AuthError
from factotum.db.pool import Database
from factotum.hub.api import Operations, create_app
from factotum.hub.service import HubService
from factotum.hub.settings import HubSettings
from factotum.hub.store.postgres import PostgresStore, prepare

logger = logging.getLogger("factotum.hub")


def build_app(settings: HubSettings) -> FastAPI:
    """The hub as a web application.

    Unlike every other service the hub has no development fallback for authentication: it is the
    one that mints every token, so a secret of its own is required in every environment.
    """
    secret = settings.hub_secret_bytes
    if secret is None:
        raise AuthError("the hub needs a token secret of its own: set FACTOTUM_HUB_TOKEN_SECRET")
    authenticator = HubAuthenticator(secret)
    holder: dict[str, Database] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = await Database.connect(settings)
        holder["database"] = database
        try:
            await prepare(database)
            store = PostgresStore(database)
            service = HubService(store, secret=secret, session_hours=settings.hub_session_hours)
            made = await service.bootstrap(
                username=settings.hub_admin_username, password=settings.hub_admin_password
            )
            if made:
                logger.warning(
                    "a tenant was created with an administrator whose password must be changed "
                    "at the first login",
                    extra={"username": settings.hub_admin_username},
                )
            app.state.operations = Operations(service=service)
            yield
        finally:
            await database.close()

    async def ready() -> bool:
        database = holder.get("database")
        return database is not None and await database.ping()

    return create_app(
        authenticator, docs=not settings.is_production, lifespan=lifespan, ready=ready
    )
