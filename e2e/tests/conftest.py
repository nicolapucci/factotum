"""Every test of this folder runs the whole house in three ways: with the services keeping what they
keep in memory and a recording sent to the edge whole in one frame, the same with the recording
streamed as a person talks (ADR 0022), and in the Postgres of ``docker compose up`` with it streamed
(an integration test, skipped where there is none). What a store does and how a recording gets to
the edge have nothing to do with each other, so they are not run in every combination."""

import pytest

from factotum.e2e import devices, house
from factotum.e2e.devices import Carrier
from factotum.e2e.stores import Store


@pytest.fixture(
    autouse=True,
    params=[
        pytest.param((Store.MEMORY, Carrier.CLIP), id="memory-clip"),
        pytest.param((Store.MEMORY, Carrier.STREAM), id="memory-stream"),
        pytest.param(
            (Store.POSTGRES, Carrier.STREAM), id="postgres-stream", marks=pytest.mark.integration
        ),
    ],
)
def _how_the_house_is_run(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> tuple[Store, Carrier]:
    store, carrier = request.param
    monkeypatch.setattr(house, "DEFAULT_STORE", store)
    monkeypatch.setattr(devices, "DEFAULT_CARRIER", carrier)
    return store, carrier
