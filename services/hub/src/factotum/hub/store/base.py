"""What the hub keeps, and the interface it is kept behind.

Tenants and their users, their devices (with a system device's credential, as a hash) and the
bindings of a skill to a device. A tenant's spaces, linked accounts, model bindings and the secrets
of its skills are later slices of the hub.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from factotum.contracts.devices import ExternalDeviceRecord, JsonScalar, SystemDeviceRecord
from factotum.contracts.identity import Role
from factotum.contracts.ids import BindingId, DeviceId, TenantId, UserId


@dataclass(frozen=True, slots=True)
class StoredUser:
    id: UserId
    tenant_id: TenantId
    username: str
    password_hash: str
    roles: frozenset[Role]
    must_change_password: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoredDevice:
    record: SystemDeviceRecord | ExternalDeviceRecord
    credential_hash: str | None
    """Only a system device has one: what it presents is checked against this, never kept in the
    clear."""
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoredBinding:
    id: BindingId
    tenant_id: TenantId
    skill: str
    device_id: DeviceId
    params: dict[str, JsonScalar]
    enabled: bool
    created_at: datetime


class HubStore(Protocol):
    async def tenant_count(self) -> int:
        """How many tenants exist. Zero means the hub has never been started before."""
        ...

    async def bootstrap(self, tenant_id: TenantId, admin: StoredUser) -> None:
        """The first tenant, with its one administrator. Called only while ``tenant_count`` is
        zero, so it does not need to be safe against a tenant that is already there."""
        ...

    async def find_user_by_username(self, username: str) -> StoredUser | None:
        """Across every tenant: there is only one until a second tenant can be made (not in slice
        7a). Logging in uses this to find which tenant a username belongs to; nothing else should,
        once a username is no longer unique across the whole hub."""
        ...

    async def get_user_by_username(
        self, tenant_id: TenantId, username: str
    ) -> StoredUser | None: ...

    async def get_user(self, tenant_id: TenantId, user_id: UserId) -> StoredUser | None: ...

    async def create_user(self, user: StoredUser) -> StoredUser | None:
        """Nothing when the tenant already has a user of this name: no user was added."""
        ...

    async def list_users(self, tenant_id: TenantId) -> list[StoredUser]:
        """By when they were created, the earliest first."""
        ...

    async def set_password(self, tenant_id: TenantId, user_id: UserId, password_hash: str) -> bool:
        """The new hash, and the user no longer has to change it. False when there is no such
        user."""
        ...

    # ---- devices

    async def create_device(self, device: StoredDevice) -> bool:
        """False when the tenant already has a device of this name: nothing was added."""
        ...

    async def get_device(self, tenant_id: TenantId, device_id: DeviceId) -> StoredDevice | None: ...

    async def list_devices(self, tenant_id: TenantId) -> list[StoredDevice]:
        """By when they were registered, the earliest first."""
        ...

    # ---- bindings

    async def create_binding(self, binding: StoredBinding) -> bool:
        """False when the skill is already bound to this device: nothing was added."""
        ...

    async def list_bindings(self, tenant_id: TenantId) -> list[StoredBinding]:
        """By when they were made, the earliest first."""
        ...
