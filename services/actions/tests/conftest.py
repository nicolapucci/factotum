"""The stores the same tests run against."""

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from factotum.actions.store import ActionsStore, InMemoryStore
from factotum.actions.store.postgres import PostgresStore, prepare
from factotum.db.pool import Database
from factotum.db.settings import DatabaseSettings


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def actions_database(database_settings: DatabaseSettings) -> AsyncIterator[Database]:
    """A database of its own for each test module, with the service's tables in it."""
    database = await Database.connect(database_settings)
    await prepare(database)
    yield database
    await database.close()


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.integration)])
def store(request: pytest.FixtureRequest) -> ActionsStore:
    """A store to run the contract tests against: each implementation, in turn. A module that uses
    it needs ``pytest.mark.asyncio(loop_scope="module")``, for the sake of the database. Every test
    works with tenants of its own, so the tests of a module do not meet."""
    if request.param == "memory":
        return InMemoryStore()
    database: Database = request.getfixturevalue("actions_database")
    return PostgresStore(database)
