"""The store in Postgres, in the schema ``hub``."""

import json
from pathlib import Path

import asyncpg
from pydantic import TypeAdapter

from factotum.contracts.devices import DeviceRecord, ExternalDeviceRecord, SystemDeviceRecord
from factotum.contracts.identity import Owner, OwnerKind, Role
from factotum.contracts.ids import BindingId, DeviceId, TenantId, UserId
from factotum.contracts.models import ModelBinding
from factotum.db.migrations import load_migrations, migrate
from factotum.db.pool import Database
from factotum.hub.store.base import (
    StoredBinding,
    StoredDevice,
    StoredModelBinding,
    StoredSecret,
    StoredUser,
)

SCHEMA = "hub"
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"

_USER_COLUMNS = "id, tenant_id, username, password_hash, roles, must_change_password, created_at"
_DEVICE_COLUMNS = "id, tenant_id, kind, name, hardware_id, record, credential_hash, created_at"
_BINDING_COLUMNS = "id, tenant_id, skill, device_id, params, enabled, created_at"
_MODEL_BINDING_COLUMNS = "id, tenant_id, owner_kind, owner_id, role, binding, created_at"
_SECRET_COLUMNS = "tenant_id, owner_kind, owner_id, name, value, created_at"  # noqa: S105

_device_adapter: TypeAdapter[SystemDeviceRecord | ExternalDeviceRecord] = TypeAdapter(DeviceRecord)


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


def _device(row: "asyncpg.Record") -> StoredDevice:
    return StoredDevice(
        record=_device_adapter.validate_json(row["record"]),
        credential_hash=row["credential_hash"],
        created_at=row["created_at"],
    )


def _binding(row: "asyncpg.Record") -> StoredBinding:
    return StoredBinding(
        id=BindingId(row["id"]),
        tenant_id=TenantId(row["tenant_id"]),
        skill=row["skill"],
        device_id=DeviceId(row["device_id"]),
        params=json.loads(row["params"]),
        enabled=row["enabled"],
        created_at=row["created_at"],
    )


def _model_binding(row: "asyncpg.Record") -> StoredModelBinding:
    return StoredModelBinding(
        binding=ModelBinding.model_validate_json(row["binding"]), created_at=row["created_at"]
    )


def _secret(row: "asyncpg.Record") -> StoredSecret:
    return StoredSecret(
        tenant_id=TenantId(row["tenant_id"]),
        name=row["name"],
        owner=Owner(kind=OwnerKind(row["owner_kind"]), id=row["owner_id"]),
        value=row["value"],
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

    # ---- devices

    async def create_device(self, device: StoredDevice) -> bool:
        tenant_id = device.record.tenant_id
        hardware_id = (
            device.record.hardware_id if isinstance(device.record, SystemDeviceRecord) else None
        )
        try:
            async with self._db.transaction(tenant_id) as connection:
                row = await connection.fetchrow(
                    f"INSERT INTO hub.devices ({_DEVICE_COLUMNS}) "  # noqa: S608 - a constant
                    "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8) "
                    "ON CONFLICT (tenant_id, name) DO NOTHING RETURNING id",
                    device.record.id,
                    tenant_id,
                    device.record.kind.value,
                    device.record.name,
                    hardware_id,
                    device.record.model_dump_json(),
                    device.credential_hash,
                    device.created_at,
                )
        except asyncpg.UniqueViolationError:
            # The name was free, but the hardware id (the other way two devices collide) was not.
            return False
        return row is not None

    async def get_device(self, tenant_id: TenantId, device_id: DeviceId) -> StoredDevice | None:
        async with self._db.transaction(tenant_id) as connection:
            row = await connection.fetchrow(
                f"SELECT {_DEVICE_COLUMNS} FROM hub.devices "  # noqa: S608 - a constant
                "WHERE tenant_id = $1 AND id = $2",
                tenant_id,
                device_id,
            )
        return None if row is None else _device(row)

    async def list_devices(self, tenant_id: TenantId) -> list[StoredDevice]:
        async with self._db.transaction(tenant_id) as connection:
            rows = await connection.fetch(
                f"SELECT {_DEVICE_COLUMNS} FROM hub.devices "  # noqa: S608 - a constant
                "WHERE tenant_id = $1 ORDER BY created_at",
                tenant_id,
            )
        return [_device(row) for row in rows]

    # ---- bindings

    async def create_binding(self, binding: StoredBinding) -> bool:
        async with self._db.transaction(binding.tenant_id) as connection:
            row = await connection.fetchrow(
                f"INSERT INTO hub.bindings ({_BINDING_COLUMNS}) "  # noqa: S608 - a constant
                "VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7) "
                "ON CONFLICT (tenant_id, skill, device_id) DO NOTHING RETURNING id",
                binding.id,
                binding.tenant_id,
                binding.skill,
                binding.device_id,
                json.dumps(binding.params),
                binding.enabled,
                binding.created_at,
            )
        return row is not None

    async def list_bindings(self, tenant_id: TenantId) -> list[StoredBinding]:
        async with self._db.transaction(tenant_id) as connection:
            rows = await connection.fetch(
                f"SELECT {_BINDING_COLUMNS} FROM hub.bindings "  # noqa: S608 - a constant
                "WHERE tenant_id = $1 ORDER BY created_at",
                tenant_id,
            )
        return [_binding(row) for row in rows]

    # ---- model bindings

    async def upsert_model_binding(self, binding: StoredModelBinding) -> None:
        owner = binding.binding.owner
        async with self._db.transaction(binding.binding.tenant_id) as connection:
            await connection.execute(
                f"INSERT INTO hub.model_bindings ({_MODEL_BINDING_COLUMNS}) "  # noqa: S608
                "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7) "
                "ON CONFLICT (tenant_id, owner_kind, owner_id, role) "
                "DO UPDATE SET id = EXCLUDED.id, binding = EXCLUDED.binding, "
                "created_at = EXCLUDED.created_at",
                binding.binding.id,
                binding.binding.tenant_id,
                owner.kind.value,
                owner.id,
                binding.binding.role.value,
                binding.binding.model_dump_json(),
                binding.created_at,
            )

    async def list_model_bindings(self, tenant_id: TenantId) -> list[StoredModelBinding]:
        async with self._db.transaction(tenant_id) as connection:
            rows = await connection.fetch(
                f"SELECT {_MODEL_BINDING_COLUMNS} FROM hub.model_bindings "  # noqa: S608
                "WHERE tenant_id = $1 ORDER BY created_at",
                tenant_id,
            )
        return [_model_binding(row) for row in rows]

    # ---- secrets

    async def upsert_secret(self, secret: StoredSecret) -> None:
        async with self._db.transaction(secret.tenant_id) as connection:
            await connection.execute(
                f"INSERT INTO hub.secrets ({_SECRET_COLUMNS}) "  # noqa: S608 - a constant
                "VALUES ($1, $2, $3, $4, $5, $6) "
                "ON CONFLICT (tenant_id, owner_kind, owner_id, name) "
                "DO UPDATE SET value = EXCLUDED.value, created_at = EXCLUDED.created_at",
                secret.tenant_id,
                secret.owner.kind.value,
                secret.owner.id,
                secret.name,
                secret.value,
                secret.created_at,
            )

    async def list_secrets(self, tenant_id: TenantId) -> list[StoredSecret]:
        async with self._db.transaction(tenant_id) as connection:
            rows = await connection.fetch(
                f"SELECT {_SECRET_COLUMNS} FROM hub.secrets "  # noqa: S608 - a constant
                "WHERE tenant_id = $1 ORDER BY created_at",
                tenant_id,
            )
        return [_secret(row) for row in rows]
