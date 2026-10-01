"""Putting the edge together from its settings: the devices it knows, the conversation service it
asks, and the connections it holds.

Authentication is the hub's once a token secret is configured (``FACTOTUM_HUB_TOKEN_SECRET``);
otherwise the development authenticator, which refuses to exist outside development and tests, so
without one in production this raises and the edge does not start. The registry of devices is still
only a file until the hub registers and pairs them (slice 7b).
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from factotum.common.devauth import DEV_TENANT_ID, DEV_USER_ID
from factotum.common.hubauth import authenticator_for, principal_delegation_for
from factotum.edge.api import create_app
from factotum.edge.conversation import ConversationClient
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
    authenticator = authenticator_for(settings, tenant_id=DEV_TENANT_ID, user_id=DEV_USER_ID)
    delegation = principal_delegation_for(settings)
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
