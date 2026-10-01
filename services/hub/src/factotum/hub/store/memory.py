"""The store in memory: what the tests of everything above it use, and the reference for what the
Postgres store has to do (the same tests run against both)."""

from dataclasses import replace
from uuid import UUID

from factotum.contracts.devices import SystemDeviceRecord
from factotum.contracts.identity import OwnerKind
from factotum.contracts.ids import BindingId, DeviceId, TenantId, UserId
from factotum.contracts.models import ModelRole
from factotum.hub.store.base import (
    StoredBinding,
    StoredDevice,
    StoredModelBinding,
    StoredSecret,
    StoredUser,
)

_ModelBindingKey = tuple[TenantId, OwnerKind, UUID, ModelRole]
_SecretKey = tuple[TenantId, OwnerKind, UUID, str]


def _model_binding_key(stored: StoredModelBinding) -> _ModelBindingKey:
    binding = stored.binding
    return (binding.tenant_id, binding.owner.kind, binding.owner.id, binding.role)


def _secret_key(stored: StoredSecret) -> _SecretKey:
    return (stored.tenant_id, stored.owner.kind, stored.owner.id, stored.name)


class InMemoryStore:
    def __init__(self) -> None:
        self._tenants: set[TenantId] = set()
        self._users: dict[UserId, StoredUser] = {}
        self._devices: dict[DeviceId, StoredDevice] = {}
        self._bindings: dict[BindingId, StoredBinding] = {}
        self._model_bindings: dict[_ModelBindingKey, StoredModelBinding] = {}
        self._secrets: dict[_SecretKey, StoredSecret] = {}

    async def tenant_count(self) -> int:
        return len(self._tenants)

    async def bootstrap(self, tenant_id: TenantId, admin: StoredUser) -> None:
        self._tenants.add(tenant_id)
        self._users[admin.id] = admin

    async def find_user_by_username(self, username: str) -> StoredUser | None:
        return next((user for user in self._users.values() if user.username == username), None)

    async def get_user_by_username(self, tenant_id: TenantId, username: str) -> StoredUser | None:
        return next(
            (
                user
                for user in self._users.values()
                if user.tenant_id == tenant_id and user.username == username
            ),
            None,
        )

    async def get_user(self, tenant_id: TenantId, user_id: UserId) -> StoredUser | None:
        user = self._users.get(user_id)
        return user if user is not None and user.tenant_id == tenant_id else None

    async def create_user(self, user: StoredUser) -> StoredUser | None:
        if await self.get_user_by_username(user.tenant_id, user.username) is not None:
            return None
        self._users[user.id] = user
        return user

    async def list_users(self, tenant_id: TenantId) -> list[StoredUser]:
        return sorted(
            (user for user in self._users.values() if user.tenant_id == tenant_id),
            key=lambda user: user.created_at,
        )

    async def set_password(self, tenant_id: TenantId, user_id: UserId, password_hash: str) -> bool:
        user = await self.get_user(tenant_id, user_id)
        if user is None:
            return False
        self._users[user_id] = replace(
            user, password_hash=password_hash, must_change_password=False
        )
        return True

    # ---- devices

    async def create_device(self, device: StoredDevice) -> bool:
        tenant_id, name = device.record.tenant_id, device.record.name
        hardware_id = (
            device.record.hardware_id if isinstance(device.record, SystemDeviceRecord) else None
        )
        for stored in self._devices.values():
            if stored.record.tenant_id != tenant_id:
                continue
            if stored.record.name == name:
                return False
            if (
                hardware_id is not None
                and isinstance(stored.record, SystemDeviceRecord)
                and stored.record.hardware_id == hardware_id
            ):
                return False
        self._devices[device.record.id] = device
        return True

    async def get_device(self, tenant_id: TenantId, device_id: DeviceId) -> StoredDevice | None:
        device = self._devices.get(device_id)
        return device if device is not None and device.record.tenant_id == tenant_id else None

    async def list_devices(self, tenant_id: TenantId) -> list[StoredDevice]:
        return sorted(
            (d for d in self._devices.values() if d.record.tenant_id == tenant_id),
            key=lambda d: d.created_at,
        )

    # ---- bindings

    async def create_binding(self, binding: StoredBinding) -> bool:
        taken = any(
            stored.tenant_id == binding.tenant_id
            and stored.skill == binding.skill
            and stored.device_id == binding.device_id
            for stored in self._bindings.values()
        )
        if taken:
            return False
        self._bindings[binding.id] = binding
        return True

    async def list_bindings(self, tenant_id: TenantId) -> list[StoredBinding]:
        return sorted(
            (b for b in self._bindings.values() if b.tenant_id == tenant_id),
            key=lambda b: b.created_at,
        )

    # ---- model bindings

    async def upsert_model_binding(self, binding: StoredModelBinding) -> None:
        self._model_bindings[_model_binding_key(binding)] = binding

    async def list_model_bindings(self, tenant_id: TenantId) -> list[StoredModelBinding]:
        return sorted(
            (m for m in self._model_bindings.values() if m.binding.tenant_id == tenant_id),
            key=lambda m: m.created_at,
        )

    # ---- secrets

    async def upsert_secret(self, secret: StoredSecret) -> None:
        self._secrets[_secret_key(secret)] = secret

    async def list_secrets(self, tenant_id: TenantId) -> list[StoredSecret]:
        return sorted(
            (s for s in self._secrets.values() if s.tenant_id == tenant_id),
            key=lambda s: s.created_at,
        )
