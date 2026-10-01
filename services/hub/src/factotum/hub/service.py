"""Logging in, changing a password, and the users of a tenant.

Slice 7a of the hub: a device's pairing, a tenant's spaces, its linked accounts, its model bindings
and the secrets of its skills are later slices. A single tenant is created, with its administrator,
the first time the hub starts (single-tenant mode only); nothing here makes a second one.
"""

import logging
from datetime import UTC, datetime
from uuid import uuid4

from factotum.common.passwords import hash_password, verify_password
from factotum.common.tokens import encode_token
from factotum.contracts.errors import (
    AuthError,
    ConflictError,
    PasswordChangeRequiredError,
    PolicyViolationError,
)
from factotum.contracts.hub import (
    ChangePasswordRequest,
    CreateUserRequest,
    LoginRequest,
    LoginResult,
    UserList,
    UserView,
)
from factotum.contracts.identity import Principal, Role, Subject, SubjectKind
from factotum.contracts.ids import TenantId, UserId
from factotum.hub.store.base import HubStore, StoredUser

logger = logging.getLogger("factotum.hub")

_WRONG_CREDENTIALS = "the username or the password is wrong"


def _view(user: StoredUser) -> UserView:
    return UserView(
        id=user.id,
        username=user.username,
        roles=user.roles,
        must_change_password=user.must_change_password,
        created_at=user.created_at,
    )


def _principal_of(user: StoredUser) -> Principal:
    return Principal(tenant_id=user.tenant_id, acts_as=Subject.user(user.id), roles=user.roles)


class HubService:
    def __init__(self, store: HubStore, *, secret: bytes, session_hours: int) -> None:
        self._store = store
        self._secret = secret
        self._session_ttl_s = session_hours * 3600

    async def bootstrap(self, *, tenant_id: TenantId, username: str, password: str) -> bool:
        """The one tenant of a single-tenant deployment, and its administrator, the first time the
        hub starts. Returns whether it was made: false when a tenant already exists, which is the
        ordinary case after the first time. Multi-tenant mode has no first tenant to make here: a
        deployment of its own calls this once, for its own first tenant, or not at all until it has
        a sign-up of its own.
        """
        if await self._store.tenant_count() > 0:
            return False
        admin = StoredUser(
            id=UserId(uuid4()),
            tenant_id=tenant_id,
            username=username,
            password_hash=hash_password(password),
            roles=frozenset({Role.ADMIN}),
            must_change_password=True,
            created_at=datetime.now(UTC),
        )
        await self._store.bootstrap(admin.tenant_id, admin)
        logger.info("the tenant was created, with its administrator")
        return True

    async def login(self, body: LoginRequest) -> LoginResult:
        user = await self._store.find_user_by_username(body.username)
        if user is None or not verify_password(body.password, user.password_hash):
            # The same error either way: which part was wrong is not said.
            raise AuthError(_WRONG_CREDENTIALS)
        now = int(datetime.now(UTC).timestamp())
        token = encode_token(
            _principal_of(user), secret=self._secret, ttl_s=self._session_ttl_s, now=now
        )
        return LoginResult(
            token=token,
            must_change_password=user.must_change_password,
            expires_at=datetime.fromtimestamp(now + self._session_ttl_s, tz=UTC),
        )

    async def me(self, principal: Principal) -> UserView:
        return _view(await self._user_of(principal))

    async def change_password(self, principal: Principal, body: ChangePasswordRequest) -> None:
        user = await self._user_of(principal)
        if not verify_password(body.current_password, user.password_hash):
            raise AuthError(_WRONG_CREDENTIALS)
        await self._store.set_password(user.tenant_id, user.id, hash_password(body.new_password))

    async def create_user(self, principal: Principal, body: CreateUserRequest) -> UserView:
        await self._admin(principal)
        created = StoredUser(
            id=UserId(uuid4()),
            tenant_id=principal.tenant_id,
            username=body.username,
            password_hash=hash_password(body.password),
            roles=body.roles,
            must_change_password=True,
            created_at=datetime.now(UTC),
        )
        stored = await self._store.create_user(created)
        if stored is None:
            raise ConflictError(f"{body.username!r} is already a user of this tenant")
        return _view(stored)

    async def list_users(self, principal: Principal) -> UserList:
        await self._admin(principal)
        users = await self._store.list_users(principal.tenant_id)
        return UserList(users=tuple(_view(user) for user in users))

    async def _user_of(self, principal: Principal) -> StoredUser:
        if principal.acts_as.kind is not SubjectKind.USER:
            raise AuthError("this is not a user's session")
        user = await self._store.get_user(principal.tenant_id, UserId(principal.acts_as.id))
        if user is None:
            raise AuthError("the user of this token no longer exists")
        return user

    async def _admin(self, principal: Principal) -> StoredUser:
        user = await self._user_of(principal)
        if Role.ADMIN not in principal.roles:
            raise PolicyViolationError("only an administrator may do this")
        if user.must_change_password:
            raise PasswordChangeRequiredError(
                "change the password of this account before doing anything else"
            )
        return user
