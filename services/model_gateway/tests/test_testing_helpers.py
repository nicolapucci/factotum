"""The doubles are part of what other tests rely on: they must not fail silently."""

import pytest

from factotum.contracts.chat import FinishEvent, FinishReason
from factotum.contracts.models import ProviderKind
from factotum.contracts.testing import make_model_binding, make_tenant_id
from factotum.model_gateway.testing import ScriptedAdapter, Sleeps, TrackedClients, ask, resolved


async def test_a_tracked_client_reaches_nothing_and_says_so_loudly() -> None:
    clients = TrackedClients()
    client = clients(resolved(make_model_binding(tenant_id=make_tenant_id())))

    with pytest.raises(AssertionError, match=r"unexpected request to http://example\.org/"):
        await client.get("http://example.org/")


async def test_tracked_clients_are_remembered() -> None:
    clients = TrackedClients()
    binding = resolved(make_model_binding(tenant_id=make_tenant_id()))

    first, second = clients(binding), clients(binding)

    assert clients.created == [first, second]


async def test_sleeps_do_not_wait_and_remember_the_delays() -> None:
    sleeps = Sleeps()

    await sleeps(1.5)
    await sleeps(3.0)

    assert sleeps.delays == [1.5, 3.0]


async def test_a_scripted_adapter_that_runs_out_of_script_fails_loudly() -> None:
    adapter = ScriptedAdapter()
    binding = resolved(make_model_binding(tenant_id=make_tenant_id()))
    clients = TrackedClients()

    with pytest.raises(IndexError):
        await adapter.chat(clients(binding), binding, ask())


async def test_a_scripted_stream_records_how_it_ended() -> None:
    adapter = ScriptedAdapter(ProviderKind.OPENAI).will_stream(
        FinishEvent(reason=FinishReason.STOP)
    )
    binding = resolved(make_model_binding(tenant_id=make_tenant_id()))
    clients = TrackedClients()

    events = [event async for event in adapter.chat_stream(clients(binding), binding, ask())]

    assert events == [FinishEvent(reason=FinishReason.STOP)]
    assert adapter.stream_endings == ["completed"]
    assert adapter.kind is ProviderKind.OPENAI
