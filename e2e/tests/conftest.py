"""Every test of this folder runs where the services keep what they keep, twice: in memory, and in
the Postgres of ``docker compose up`` (an integration test, skipped where there is none)."""

import pytest

from factotum.e2e import house
from factotum.e2e.stores import Store


@pytest.fixture(
    autouse=True, params=[Store.MEMORY, pytest.param(Store.POSTGRES, marks=pytest.mark.integration)]
)
def _where_the_services_keep_what_they_keep(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Store:
    store: Store = request.param
    monkeypatch.setattr(house, "DEFAULT_STORE", store)
    return store
