"""What the hub keeps, and the interface it is kept behind.

Slice 7a only: tenants and their users. A device's pairing, a tenant's spaces, its linked accounts,
its model bindings and the secrets of its skills are later slices of the hub.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from factotum.contracts.identity import Role
from factotum.contracts.ids import TenantId, UserId


@dataclass(frozen=True, slots=True)
class StoredUser:
    id: UserId
    tenant_id: TenantId
    username: str
    password_hash: str
    roles: frozenset[Role]
    must_change_password: bool
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
