"""Tests against a real Postgres, without touching the developer's own data.

Each test module gets a database of its own, with the vector extension, created before it and
dropped after it, on the server the settings point at (the one ``docker compose up`` starts). If
there is no server the tests are skipped with a message that says how to start it; CI sets
``FACTOTUM_REQUIRE_INTEGRATION=1``, and there a missing server is a failure, never a silent skip.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import NoReturn
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from factotum.db.settings import DatabaseNotConfiguredError, DatabaseSettings

REQUIRE_VARIABLE = "FACTOTUM_REQUIRE_INTEGRATION"


def server_settings() -> DatabaseSettings:
    """The server to test against, from the environment (never from a ``.env`` file)."""
    return DatabaseSettings(_env_file=None)


async def _admin_connection(settings: DatabaseSettings) -> "asyncpg.Connection[asyncpg.Record]":
    return await asyncpg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=settings.password(),
        database="postgres",
        timeout=5,
    )


@asynccontextmanager
async def temporary_database(
    settings: DatabaseSettings | None = None,
) -> AsyncIterator[DatabaseSettings]:
    """A database that exists only inside the block; yields the settings that point at it."""
    server = settings or server_settings()
    name = f"factotum_test_{uuid4().hex[:12]}"
    admin = await _admin_connection(server)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        mine = server.model_copy(update={"postgres_db": name})
        inside = await asyncpg.connect(
            host=mine.postgres_host,
            port=mine.postgres_port,
            user=mine.postgres_user,
            password=mine.password(),
            database=name,
        )
        try:
            await inside.execute("CREATE EXTENSION IF NOT EXISTS vector")
        finally:
            await inside.close()
        try:
            yield mine
        finally:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await admin.close()


def skip_or_fail(error: Exception) -> NoReturn:
    """There is no database to test against: skip, or fail where a skip would hide a bad setup."""
    message = (
        f"no Postgres to test against ({type(error).__name__}): start it with "
        "`docker compose up -d --wait` and set FACTOTUM_POSTGRES_PASSWORD"
    )
    if os.environ.get(REQUIRE_VARIABLE) == "1":
        pytest.fail(message)
    pytest.skip(message)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def database_settings() -> AsyncIterator[DatabaseSettings]:
    """Settings for a fresh database, one per test module. A module that uses it also needs
    ``pytest.mark.asyncio(loop_scope="module")``, because a pool belongs to one event loop."""
    try:
        async with temporary_database() as settings:
            yield settings
    except (  # pragma: no cover - only runs when there is no database
        OSError,
        asyncpg.PostgresError,
        TimeoutError,
        DatabaseNotConfiguredError,
    ) as error:
        skip_or_fail(error)
