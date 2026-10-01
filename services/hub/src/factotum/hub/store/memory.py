"""The store in memory: what the tests of everything above it use, and the reference for what the
Postgres store has to do (the same tests run against both)."""

from dataclasses import replace

from factotum.contracts.ids import TenantId, UserId
from factotum.hub.store.base import StoredUser


class InMemoryStore:
    def __init__(self) -> None:
        self._tenants: set[TenantId] = set()
        self._users: dict[UserId, StoredUser] = {}

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
