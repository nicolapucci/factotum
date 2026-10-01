"""Putting the edge together from its settings: the devices it knows, the conversation service it
asks, and the connections it holds.

Authentication is the hub's once a token secret is configured (``FACTOTUM_HUB_TOKEN_SECRET``);
otherwise the development authenticator, which refuses to exist outside development and tests, so
without one in production this raises and the edge does not start. The registry of devices follows
the same switch: the hub's, fetched once in the lifespan, once ``FACTOTUM_EDGE_HUB_URL`` is also
set; otherwise still a file, for development.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from fastapi import FastAPI

from factotum.common.devauth import DEV_TENANT_ID, DEV_USER_ID
from factotum.common.hubauth import authenticator_for, principal_delegation_for
from factotum.contracts.identity import SINGLE_TENANT_ID
from factotum.edge.api import create_app
from factotum.edge.conversation import ConversationClient
from factotum.edge.registry import (
    DeviceRegistry,
    FileDeviceRegistry,
    HubDeviceRegistry,
    load_devices_file,
)
from factotum.edge.router import Sessions
from factotum.edge.session import SessionSettings
from factotum.edge.settings import EdgeSettings

logger = logging.getLogger("factotum.edge")


def _file_registry(settings: EdgeSettings) -> DeviceRegistry:
    if settings.edge_devices_file is None:
        logger.warning("no file of devices is set: no device can connect")
        return FileDeviceRegistry(settings, [])
    return load_devices_file(
        settings.edge_devices_file, settings, tenant_id=DEV_TENANT_ID, user_id=DEV_USER_ID
    )


def build_app(settings: EdgeSettings) -> FastAPI:
    authenticator = authenticator_for(settings, tenant_id=DEV_TENANT_ID, user_id=DEV_USER_ID)
    delegation = principal_delegation_for(settings)
    sessions = Sessions()
    client = httpx.AsyncClient(
        base_url=str(settings.edge_conversation_url),
        timeout=httpx.Timeout(connect=3.0, read=settings.edge_turn_timeout_s, write=10.0, pool=5.0),
    )
    hub_client: httpx.AsyncClient | None = None
    hub_registry: HubDeviceRegistry | None = None
    registry: DeviceRegistry
    if settings.hub_secret_bytes is not None and settings.edge_hub_url is not None:
        hub_client = httpx.AsyncClient(base_url=str(settings.edge_hub_url))
        hub_registry = HubDeviceRegistry(hub_client, delegation, SINGLE_TENANT_ID)
        registry = hub_registry
    else:
        registry = _file_registry(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            stack.push_async_callback(sessions.close_all)
            stack.push_async_callback(client.aclose)
            if hub_client is not None and hub_registry is not None:
                stack.push_async_callback(hub_client.aclose)
                await hub_registry.reload()
            yield

    return create_app(
        authenticator,
        sessions=sessions,
        registry=registry,
        conversation=ConversationClient(client, delegation),
        session_settings=SessionSettings.of(settings),
        docs=not settings.is_production,
        lifespan=lifespan,
    )
