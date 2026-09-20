"""A connection pool whose transactions know which tenant they work for.

Every tenant-scoped query is written with ``WHERE tenant_id = ...``. The transaction also says the
tenant to the database (``app.tenant_id``, local to the transaction), which is what row-level
security policies will read when a deployment is shared by tenants: turning them on is then a
migration, not a rewrite.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Self

import asyncpg

from factotum.contracts.ids import TenantId
from factotum.db.settings import DatabaseSettings
from factotum.db.types import Connection


class Database:
    def __init__(self, pool: "asyncpg.Pool[asyncpg.Record]") -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, settings: DatabaseSettings, *, min_size: int = 1) -> Self:
        pool = await asyncpg.create_pool(
            host=settings.postgres_host,
            port=settings.postgres_port,
            user=settings.postgres_user,
            password=settings.password(),
            database=settings.postgres_db,
            min_size=min_size,
            max_size=max(min_size, settings.postgres_pool_max),
            command_timeout=60,
        )
        if pool is None:  # pragma: no cover - asyncpg only returns None for a closed pool
            raise RuntimeError("the connection pool could not be created")
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Connection]:
        """A connection for a statement that needs no transaction of its own."""
        async with self._pool.acquire() as connection:
            yield connection

    @asynccontextmanager
    async def transaction(self, tenant_id: TenantId | None = None) -> AsyncIterator[Connection]:
        """A transaction; committed when the block ends, rolled back when it raises.

        With a ``tenant_id`` the database is told whom it is working for, for as long as the
        transaction lasts.
        """
        async with self._pool.acquire() as connection, connection.transaction():
            if tenant_id is not None:
                await connection.execute(
                    "SELECT set_config('app.tenant_id', $1, true)", str(tenant_id)
                )
            yield connection

    async def ping(self) -> bool:
        """Whether the database answers."""
        try:
            async with self._pool.acquire() as connection:
                return bool(await connection.fetchval("SELECT 1") == 1)
        except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError):
            return False
