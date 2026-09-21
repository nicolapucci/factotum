"""Putting the edge together from its settings: the devices it knows, the conversation service it
asks, and the connections it holds.

Until the hub issues real credentials the only way to authenticate the HTTP interface is the
development authenticator, and the only registry of devices is a file: both refuse to exist outside
development and tests, so in production this raises, and the edge does not start.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from factotum.common.devauth import DEV_TENANT_ID, DEV_USER_ID, DevAuthenticator
from factotum.edge.api import create_app
from factotum.edge.conversation import ConversationClient, DevPrincipalDelegation
from factotum.edge.registry import DeviceRegistry, FileDeviceRegistry, load_devices_file
from factotum.edge.router import Sessions
from factotum.edge.session import SessionSettings
from factotum.edge.settings import EdgeSettings

logger = logging.getLogger("factotum.edge")


def _registry(settings: EdgeSettings) -> DeviceRegistry:
    if settings.edge_devices_file is None:
        logger.warning("no file of devices is set: no device can connect")
        return FileDeviceRegistry(settings, [])
    return load_devices_file(
        settings.edge_devices_file, settings, tenant_id=DEV_TENANT_ID, user_id=DEV_USER_ID
    )


def build_app(settings: EdgeSettings) -> FastAPI:
    authenticator = DevAuthenticator(settings, tenant_id=DEV_TENANT_ID, user_id=DEV_USER_ID)
    delegation = DevPrincipalDelegation(settings)
    registry = _registry(settings)
    sessions = Sessions()
    client = httpx.AsyncClient(
        base_url=str(settings.edge_conversation_url),
        timeout=httpx.Timeout(connect=3.0, read=settings.edge_turn_timeout_s, write=10.0, pool=5.0),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await sessions.close_all()
            await client.aclose()

    return create_app(
        authenticator,
        sessions=sessions,
        registry=registry,
        conversation=ConversationClient(client, delegation),
        session_settings=SessionSettings.of(settings),
        docs=not settings.is_production,
        lifespan=lifespan,
    )
