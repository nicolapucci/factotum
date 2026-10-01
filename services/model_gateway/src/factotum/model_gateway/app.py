"""Putting the gateway together from its settings."""

import httpx
from fastapi import FastAPI

from factotum.common.devauth import DEV_TENANT_ID, DEV_USER_ID
from factotum.common.hubauth import authenticator_for, principal_delegation_for
from factotum.common.urlguard import UrlPolicy
from factotum.contracts.identity import SINGLE_TENANT_ID, TenantMode
from factotum.model_gateway.api import create_app
from factotum.model_gateway.bindings import (
    HubBindingRepository,
    InMemoryBindingRepository,
    load_bindings_file,
)
from factotum.model_gateway.core import ModelGateway, guarded_clients
from factotum.model_gateway.secrets import EnvSecretResolver
from factotum.model_gateway.settings import GatewaySettings


def provider_url_policy(settings: GatewaySettings) -> UrlPolicy:
    """Which networks the gateway may reach to call a provider.

    A single-tenant (on-premise) deployment is its owner's: the models may be on the machine or on
    the LAN, which is where an Ollama usually is. A multi-tenant deployment shares its network with
    others, so an address that points inside it is refused: only the public internet.
    """
    if settings.tenant_mode is TenantMode.SINGLE_TENANT:
        return UrlPolicy.private_networks_allowed()
    return UrlPolicy.public_only()


def build_gateway(settings: GatewaySettings) -> ModelGateway:
    repository = InMemoryBindingRepository()
    if settings.gateway_bindings_file is not None:
        for binding in load_bindings_file(settings.gateway_bindings_file, tenant_id=DEV_TENANT_ID):
            repository.add(binding)
    return ModelGateway(
        repository,
        EnvSecretResolver(),
        client_factory=guarded_clients(provider_url_policy(settings)),
    )


def build_app(settings: GatewaySettings) -> FastAPI:
    """The gateway as a web application.

    Authentication is the hub's once a token secret is configured (``FACTOTUM_HUB_TOKEN_SECRET``);
    otherwise the development authenticator, which refuses to exist outside development and test,
    so without one in production this raises and the service does not start. The bindings follow the
    same switch: the hub's, fetched once in the lifespan, once ``FACTOTUM_GATEWAY_HUB_URL`` is also
    set; otherwise still a file, for development.
    """
    authenticator = authenticator_for(settings, tenant_id=DEV_TENANT_ID, user_id=DEV_USER_ID)
    hub_client: httpx.AsyncClient | None = None
    hub_repository: HubBindingRepository | None = None
    if settings.hub_secret_bytes is not None and settings.gateway_hub_url is not None:
        hub_client = httpx.AsyncClient(base_url=str(settings.gateway_hub_url))
        hub_repository = HubBindingRepository(
            hub_client, principal_delegation_for(settings), SINGLE_TENANT_ID
        )
        gateway = ModelGateway(
            hub_repository,
            EnvSecretResolver(),
            client_factory=guarded_clients(provider_url_policy(settings)),
        )
    else:
        gateway = build_gateway(settings)

    async def on_start() -> None:
        if hub_repository is not None:
            await hub_repository.reload()

    async def on_stop() -> None:
        if hub_client is not None:
            await hub_client.aclose()

    return create_app(
        gateway,
        authenticator,
        docs=not settings.is_production,
        on_start=on_start,
        on_stop=on_stop,
    )
