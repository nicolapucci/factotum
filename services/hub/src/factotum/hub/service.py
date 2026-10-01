"""Logging in, changing a password, the users of a tenant, its devices, the skills bound to them,
and the models bound to a role.

A tenant's spaces, linked accounts and the secrets of its skills are later slices. A single tenant
is created, with its administrator, the first time the hub starts (single-tenant mode only); nothing
here makes a second one.
"""

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

from factotum.common.credentials import hash_credential, new_credential
from factotum.common.passwords import hash_password, verify_password
from factotum.common.tokens import encode_token
from factotum.contracts.devices import (
    DevicePolicy,
    ExternalDeviceRecord,
    SystemDeviceRecord,
)
from factotum.contracts.errors import (
    AuthError,
    ConflictError,
    NotFoundError,
    PasswordChangeRequiredError,
    PolicyViolationError,
)
from factotum.contracts.hub import (
    BindingList,
    BindingView,
    ChangePasswordRequest,
    CreateBindingRequest,
    CreateExternalDeviceRequest,
    CreateModelBindingRequest,
    CreateSystemDeviceRequest,
    CreateUserRequest,
    DeviceList,
    DeviceOwner,
    LoginRequest,
    LoginResult,
    ModelBindingList,
    ModelBindingOwner,
    ModelBindingView,
    PairedDevice,
    SystemDeviceWithCredential,
    UserList,
    UserView,
)
from factotum.contracts.identity import Owner, Principal, Role, Subject, SubjectKind
from factotum.contracts.ids import BindingId, DeviceId, ModelBindingId, TenantId, UserId
from factotum.contracts.models import ModelBinding
from factotum.hub.store.base import (
    HubStore,
    StoredBinding,
    StoredDevice,
    StoredModelBinding,
    StoredUser,
)

logger = logging.getLogger("factotum.hub")

_WRONG_CREDENTIALS = "the username or the password is wrong"


def _owner_of(principal: Principal, choice: DeviceOwner) -> Owner:
    """Only called after ``_admin``, which has already made sure ``principal`` is a person: a
    device of one's own is always that person's."""
    if choice is DeviceOwner.TENANT:
        return Owner.of_subject(Subject.tenant(principal.tenant_id))
    return Owner.of_subject(principal.acts_as)


def _default_policy(choice: DeviceOwner) -> DevicePolicy:
    kind = SubjectKind.TENANT if choice is DeviceOwner.TENANT else SubjectKind.USER
    return DevicePolicy.default_for(kind)


def _device_list(devices: Sequence[StoredDevice]) -> DeviceList:
    system: list[SystemDeviceWithCredential] = []
    external: list[ExternalDeviceRecord] = []
    for stored in devices:
        if isinstance(stored.record, SystemDeviceRecord):
            if stored.credential_hash is None:
                raise ValueError("a system device always has a credential hash")  # pragma: no cover
            system.append(
                SystemDeviceWithCredential(
                    device=stored.record, credential_hash=stored.credential_hash
                )
            )
        else:
            external.append(stored.record)
    return DeviceList(system=tuple(system), external=tuple(external))


def _binding_view(binding: StoredBinding) -> BindingView:
    return BindingView(
        id=binding.id,
        skill=binding.skill,
        device_id=binding.device_id,
        params=binding.params,
        enabled=binding.enabled,
    )


def _model_binding_owner_of(principal: Principal, choice: ModelBindingOwner) -> Owner:
    """Only called after ``_admin``, which has already made sure ``principal`` is a person: bringing
    one's own model is always that person's, never bound on a member's behalf."""
    if choice is ModelBindingOwner.TENANT:
        return Owner.of_subject(Subject.tenant(principal.tenant_id))
    return Owner.of_subject(principal.acts_as)


def _model_binding_view(binding: ModelBinding) -> ModelBindingView:
    return ModelBindingView(
        id=binding.id,
        role=binding.role,
        provider=binding.provider,
        model=binding.model,
        base_url=binding.base_url,
        owner=binding.owner,
        secret_ref=binding.secret_ref,
        options=binding.options,
    )


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

    async def create_device(
        self, principal: Principal, body: CreateSystemDeviceRequest | CreateExternalDeviceRequest
    ) -> PairedDevice | ExternalDeviceRecord:
        await self._admin(principal)
        owner = _owner_of(principal, body.owner)
        policy = body.policy or _default_policy(body.owner)
        now = datetime.now(UTC)
        if isinstance(body, CreateSystemDeviceRequest):
            system = SystemDeviceRecord(
                id=DeviceId(uuid4()),
                tenant_id=principal.tenant_id,
                name=body.name,
                hardware_id=body.hardware_id,
                owner=owner,
                policy=policy,
                capabilities=body.capabilities,
                display=body.display,
                aliases=body.aliases,
                tags=body.tags,
            )
            token = new_credential()
            added = await self._store.create_device(
                StoredDevice(record=system, credential_hash=hash_credential(token), created_at=now)
            )
            if not added:
                raise ConflictError(f"{body.name!r} is already a device of this tenant")
            return PairedDevice(device=system, token=token)
        external = ExternalDeviceRecord(
            id=DeviceId(uuid4()),
            tenant_id=principal.tenant_id,
            name=body.name,
            owner=owner,
            policy=policy,
            aliases=body.aliases,
            tags=body.tags,
            sensitivity=body.sensitivity,
            external_refs=body.external_refs,
        )
        added = await self._store.create_device(
            StoredDevice(record=external, credential_hash=None, created_at=now)
        )
        if not added:
            raise ConflictError(f"{body.name!r} is already a device of this tenant")
        return external

    async def list_devices(self, principal: Principal) -> DeviceList:
        """Every device of the caller's tenant: a registry or a directory built from this does not
        need to ask the hub again for every device it looks for."""
        return _device_list(await self._store.list_devices(principal.tenant_id))

    async def create_binding(self, principal: Principal, body: CreateBindingRequest) -> BindingView:
        await self._admin(principal)
        device = await self._store.get_device(principal.tenant_id, body.device_id)
        if device is None:
            raise NotFoundError(f"there is no device {body.device_id} in this tenant")
        binding = StoredBinding(
            id=BindingId(uuid4()),
            tenant_id=principal.tenant_id,
            skill=body.skill,
            device_id=body.device_id,
            params=body.params,
            enabled=body.enabled,
            created_at=datetime.now(UTC),
        )
        if not await self._store.create_binding(binding):
            raise ConflictError(f"{body.skill!r} is already bound to this device")
        return _binding_view(binding)

    async def list_bindings(self, principal: Principal) -> BindingList:
        """Every binding of the caller's tenant, the same way as ``list_devices``."""
        bindings = await self._store.list_bindings(principal.tenant_id)
        return BindingList(bindings=tuple(_binding_view(binding) for binding in bindings))

    async def upsert_model_binding(
        self, principal: Principal, body: CreateModelBindingRequest
    ) -> ModelBindingView:
        """Binds a role to a model, for the tenant or for the caller's own use, replacing whatever
        that owner already had bound to the role: changing a model is routine, not a conflict."""
        await self._admin(principal)
        binding = ModelBinding(
            id=ModelBindingId(uuid4()),
            tenant_id=principal.tenant_id,
            owner=_model_binding_owner_of(principal, body.owner),
            role=body.role,
            provider=body.provider,
            model=body.model,
            base_url=body.base_url,
            secret_ref=body.secret_ref,
            options=body.options,
        )
        await self._store.upsert_model_binding(
            StoredModelBinding(binding=binding, created_at=datetime.now(UTC))
        )
        return _model_binding_view(binding)

    async def list_model_bindings(self, principal: Principal) -> ModelBindingList:
        """Every model binding of the caller's tenant: the gateway reads this once, the same way the
        edge and the actions service read devices and bindings."""
        bindings = await self._store.list_model_bindings(principal.tenant_id)
        return ModelBindingList(
            bindings=tuple(_model_binding_view(stored.binding) for stored in bindings)
        )

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
