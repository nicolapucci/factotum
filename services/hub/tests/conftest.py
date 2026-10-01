"""The stores the same tests run against."""

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from factotum.db.pool import Database
from factotum.db.settings import DatabaseSettings
from factotum.hub.store import HubStore, InMemoryStore
from factotum.hub.store.postgres import PostgresStore, prepare


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def hub_database(database_settings: DatabaseSettings) -> AsyncIterator[Database]:
    """A database of its own for each test module, with the hub's tables in it."""
    database = await Database.connect(database_settings)
    await prepare(database)
    yield database
    await database.close()


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.integration)])
def store(request: pytest.FixtureRequest) -> HubStore:
    """A store to run the contract tests against: each implementation, in turn."""
    if request.param == "memory":
        return InMemoryStore()
    database: Database = request.getfixturevalue("hub_database")
    return PostgresStore(database)
