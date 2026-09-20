"""A call to a provider does not connect again when one was made a moment ago, and shares nothing
else with the calls before it."""

from collections.abc import AsyncIterator, Sequence

import httpx
import pytest

from factotum.common.config import Environment, ServiceSettings
from factotum.common.devauth import DevAuthenticator
from factotum.common.testing import KeepAliveServer
from factotum.common.urlguard import GuardedTransport, UrlPolicy
from factotum.contracts.embeddings import EmbeddingRequest
from factotum.contracts.models import ModelRole, ProviderKind
from factotum.contracts.testing import (
    make_model_binding,
    make_tenant_id,
    make_tenant_principal,
    make_user_id,
)
from factotum.model_gateway.api import create_app
from factotum.model_gateway.bindings import InMemoryBindingRepository
from factotum.model_gateway.core import GuardedClients, ModelGateway, guarded_clients
from factotum.model_gateway.providers.base import EmbeddingBatch, ResolvedBinding
from factotum.model_gateway.secrets import EnvSecretResolver
from factotum.model_gateway.testing import ScriptedAdapter, TrackedClients, resolved

TENANT = make_tenant_id()
PRINCIPAL = make_tenant_principal(tenant_id=TENANT)
ASK = EmbeddingRequest(inputs=("hello",))
PRIVATE_OK = UrlPolicy.private_networks_allowed()
PROVIDER = "provider.test"
"""A host name that resolves to this machine."""


async def resolve(host: str) -> Sequence[str]:
    if host != PROVIDER:
        raise OSError(f"unknown host {host}")
    return ["127.0.0.1"]


class Pinging(ScriptedAdapter):
    """An adapter whose embeddings ask the provider something, over the client it is lent."""

    async def embed(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, inputs: Sequence[str]
    ) -> EmbeddingBatch:
        response = await client.post(f"{resolved.base_url}/api/embed", json={"input": list(inputs)})
        response.raise_for_status()
        return EmbeddingBatch(vectors=tuple((0.5, 0.5) for _ in inputs))


@pytest.fixture
async def server() -> AsyncIterator[KeepAliveServer]:
    async with KeepAliveServer() as running:
        yield running


def clients_of_the_deployment() -> GuardedClients:
    return guarded_clients(PRIVATE_OK, resolver=resolve)


def gateway_to(server: KeepAliveServer, clients: GuardedClients) -> ModelGateway:
    binding = make_model_binding(
        tenant_id=TENANT,
        role=ModelRole.EMBEDDING,
        provider=ProviderKind.OLLAMA,
        base_url=f"http://{PROVIDER}:{server.port}",
    )
    return ModelGateway(
        InMemoryBindingRepository([binding]),
        EnvSecretResolver(),
        adapters=dict.fromkeys(ProviderKind, Pinging()),
        client_factory=clients,
    )


class TestCallsShareTheirConnections:
    async def test_calls_to_one_provider_share_a_connection(self, server: KeepAliveServer) -> None:
        gateway = gateway_to(server, clients_of_the_deployment())

        for _ in range(3):
            await gateway.embed(PRINCIPAL, ASK)
        await gateway.aclose()

        assert (server.connections, len(server.requests)) == (1, 3)

    async def test_every_call_gets_a_client_of_its_own_and_closing_it_keeps_the_connection(
        self, server: KeepAliveServer
    ) -> None:
        clients = clients_of_the_deployment()
        binding = make_model_binding(tenant_id=TENANT, base_url=f"http://{PROVIDER}:{server.port}")
        lent = resolved(binding)
        first, second = clients(lent), clients(lent)

        async with first:
            await first.get(server.url(PROVIDER))
        await second.get(server.url(PROVIDER))  # over the connection the first one made
        await second.aclose()
        await clients.aclose()

        assert first is not second
        assert first.is_closed
        assert server.connections == 1

    async def test_the_connections_stay_open_between_calls_and_close_with_the_gateway(
        self, server: KeepAliveServer
    ) -> None:
        gateway = gateway_to(server, clients_of_the_deployment())
        await gateway.embed(PRINCIPAL, ASK)

        assert server.open == 1

        await gateway.aclose()

        await server.until_closed()
        assert server.open == 0

    async def test_the_service_closes_the_connections_when_it_stops(
        self, server: KeepAliveServer
    ) -> None:
        gateway = gateway_to(server, clients_of_the_deployment())
        authenticator = DevAuthenticator(
            ServiceSettings(_env_file=None, environment=Environment.TEST),
            tenant_id=TENANT,
            user_id=make_user_id(),
        )
        app = create_app(gateway, authenticator)

        async with app.router.lifespan_context(app):
            await gateway.embed(PRINCIPAL, ASK)
            assert server.open == 1

        await server.until_closed()

    async def test_a_gateway_with_clients_of_another_kind_closes_without_a_word(self) -> None:
        clients = TrackedClients()
        gateway = ModelGateway(
            InMemoryBindingRepository([]), EnvSecretResolver(), client_factory=clients
        )

        await gateway.aclose()

        assert clients.created == []


class TestCallsShareNothingElse:
    async def test_what_a_provider_sets_does_not_reach_the_next_call(self) -> None:
        async with KeepAliveServer(cookie=True) as server:
            # A client that lives on sends a cookie back to the server that set it...
            transport = GuardedTransport(PRIVATE_OK, resolver=resolve)
            async with httpx.AsyncClient(transport=transport) as lasting:
                await lasting.get(server.url(PROVIDER))
                await lasting.get(server.url(PROVIDER))
            assert server.requests[1]["cookie"] == "session=1"
            server.requests.clear()

            # ...and the calls of a gateway, each with a client of its own, do not: whose cookie
            # it was is not something the next call, of another tenant, may be told.
            gateway = gateway_to(server, clients_of_the_deployment())
            await gateway.embed(PRINCIPAL, ASK)
            await gateway.embed(PRINCIPAL, ASK)
            await gateway.aclose()

            assert len(server.requests) == 2
            assert all("cookie" not in headers for headers in server.requests)
