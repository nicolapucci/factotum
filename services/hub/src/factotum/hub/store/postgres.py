"""The store in Postgres, in the schema ``hub``."""

from pathlib import Path

import asyncpg

from factotum.contracts.identity import Role
from factotum.contracts.ids import TenantId, UserId
from factotum.db.migrations import load_migrations, migrate
from factotum.db.pool import Database
from factotum.hub.store.base import StoredUser

SCHEMA = "hub"
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"

_USER_COLUMNS = "id, tenant_id, username, password_hash, roles, must_change_password, created_at"


async def prepare(database: Database) -> list[int]:
    """Bring the tables of the service up to date. Returns the migrations that were applied."""
    async with database.connection() as connection:
        return await migrate(connection, SCHEMA, load_migrations(MIGRATIONS))


def _user(row: "asyncpg.Record") -> StoredUser:
    return StoredUser(
        id=UserId(row["id"]),
        tenant_id=TenantId(row["tenant_id"]),
        username=row["username"],
        password_hash=row["password_hash"],
        roles=frozenset(Role(value) for value in row["roles"]),
        must_change_password=row["must_change_password"],
        created_at=row["created_at"],
    )


class PostgresStore:
    def __init__(self, database: Database) -> None:
        self._db = database

    @property
    def database(self) -> Database:
        return self._db

    async def tenant_count(self) -> int:
        async with self._db.connection() as connection:
            count = await connection.fetchval("SELECT count(*) FROM hub.tenants")
            return int(count)

    async def bootstrap(self, tenant_id: TenantId, admin: StoredUser) -> None:
        async with self._db.transaction(tenant_id) as connection:
            await connection.execute(
                "INSERT INTO hub.tenants (id, created_at) VALUES ($1, $2)",
                tenant_id,
                admin.created_at,
            )
            await connection.execute(
                f"INSERT INTO hub.users ({_USER_COLUMNS}) "  # noqa: S608 - a constant
                "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                admin.id,
                admin.tenant_id,
                admin.username,
                admin.password_hash,
                [role.value for role in admin.roles],
                admin.must_change_password,
                admin.created_at,
            )

    async def find_user_by_username(self, username: str) -> StoredUser | None:
        async with self._db.connection() as connection:
            row = await connection.fetchrow(
                f"SELECT {_USER_COLUMNS} FROM hub.users WHERE username = $1",  # noqa: S608
                username,
            )
        return None if row is None else _user(row)

    async def get_user_by_username(self, tenant_id: TenantId, username: str) -> StoredUser | None:
        async with self._db.transaction(tenant_id) as connection:
            row = await connection.fetchrow(
                f"SELECT {_USER_COLUMNS} FROM hub.users "  # noqa: S608 - a constant
                "WHERE tenant_id = $1 AND username = $2",
                tenant_id,
                username,
            )
        return None if row is None else _user(row)

    async def get_user(self, tenant_id: TenantId, user_id: UserId) -> StoredUser | None:
        async with self._db.transaction(tenant_id) as connection:
            row = await connection.fetchrow(
                f"SELECT {_USER_COLUMNS} FROM hub.users "  # noqa: S608 - a constant
                "WHERE tenant_id = $1 AND id = $2",
                tenant_id,
                user_id,
            )
        return None if row is None else _user(row)

    async def create_user(self, user: StoredUser) -> StoredUser | None:
        async with self._db.transaction(user.tenant_id) as connection:
            row = await connection.fetchrow(
                f"INSERT INTO hub.users ({_USER_COLUMNS}) "  # noqa: S608 - a constant
                "VALUES ($1, $2, $3, $4, $5, $6, $7) "
                "ON CONFLICT (tenant_id, username) DO NOTHING "
                f"RETURNING {_USER_COLUMNS}",
                user.id,
                user.tenant_id,
                user.username,
                user.password_hash,
                [role.value for role in user.roles],
                user.must_change_password,
                user.created_at,
            )
        return None if row is None else _user(row)

    async def list_users(self, tenant_id: TenantId) -> list[StoredUser]:
        async with self._db.transaction(tenant_id) as connection:
            rows = await connection.fetch(
                f"SELECT {_USER_COLUMNS} FROM hub.users "  # noqa: S608 - a constant
                "WHERE tenant_id = $1 ORDER BY created_at",
                tenant_id,
            )
        return [_user(row) for row in rows]

    async def set_password(self, tenant_id: TenantId, user_id: UserId, password_hash: str) -> bool:
        async with self._db.transaction(tenant_id) as connection:
            result = await connection.execute(
                "UPDATE hub.users SET password_hash = $1, must_change_password = false "
                "WHERE tenant_id = $2 AND id = $3",
                password_hash,
                tenant_id,
                user_id,
            )
        return result == "UPDATE 1"
