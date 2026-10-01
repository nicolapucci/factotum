"""What the hub's own API says: logging in, the users of a tenant, its devices and the skills
bound to them.

A tenant's spaces, linked accounts, model bindings and skill secrets are later slices of the hub.
"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from factotum.contracts.base import Contract
from factotum.contracts.capabilities import Capability, DisplayInfo
from factotum.contracts.devices import (
    DeviceKind,
    DevicePolicy,
    ExternalDeviceRecord,
    JsonScalar,
    SystemDeviceRecord,
)
from factotum.contracts.effects import Effect
from factotum.contracts.identity import Role
from factotum.contracts.ids import BindingId, DeviceId, UserId

_Username = Field(min_length=1, max_length=64)
_Password = Field(min_length=1, max_length=256)


class LoginRequest(Contract):
    username: str = _Username
    password: str = _Password


class LoginResult(Contract):
    token: str
    must_change_password: bool
    expires_at: AwareDatetime


class ChangePasswordRequest(Contract):
    current_password: str = _Password
    new_password: str = Field(min_length=8, max_length=256)


class CreateUserRequest(Contract):
    username: str = _Username
    password: str = _Password
    roles: frozenset[Role] = Field(default=frozenset({Role.MEMBER}), min_length=1)


class UserView(Contract):
    id: UserId
    username: str
    roles: frozenset[Role]
    must_change_password: bool
    created_at: AwareDatetime


class UserList(Contract):
    users: tuple[UserView, ...] = ()


class DeviceOwner(StrEnum):
    """Who a device that is registered belongs to: the tenant (shared), or the administrator who
    registers it, for their own (an administrator is always a person)."""

    TENANT = "tenant"
    USER = "user"


class CreateSystemDeviceRequest(Contract):
    kind: Literal[DeviceKind.SYSTEM] = DeviceKind.SYSTEM
    name: str = Field(min_length=1, max_length=100)
    hardware_id: str = Field(pattern=r"^[A-Za-z0-9:._-]{1,128}$")
    owner: DeviceOwner = DeviceOwner.TENANT
    capabilities: frozenset[Capability]
    display: DisplayInfo | None = None
    aliases: tuple[str, ...] = Field(default=(), max_length=20)
    tags: frozenset[str] = frozenset()
    policy: DevicePolicy | None = None
    """Left out, a shared device may trigger reversible actions and a personal one anything."""


class CreateExternalDeviceRequest(Contract):
    kind: Literal[DeviceKind.EXTERNAL] = DeviceKind.EXTERNAL
    name: str = Field(min_length=1, max_length=100)
    owner: DeviceOwner = DeviceOwner.TENANT
    sensitivity: Effect = Effect.READ_ONLY
    external_refs: dict[str, str] = Field(default_factory=dict)
    aliases: tuple[str, ...] = Field(default=(), max_length=20)
    tags: frozenset[str] = frozenset()
    policy: DevicePolicy | None = None


CreateDeviceRequest = Annotated[
    CreateSystemDeviceRequest | CreateExternalDeviceRequest, Field(discriminator="kind")
]


class PairedDevice(Contract):
    """A system device, and the credential it was given: shown once, at pairing, and never again
    (the hub keeps only its hash)."""

    device: SystemDeviceRecord
    token: str


class SystemDeviceWithCredential(Contract):
    """A system device as the registry of a service needs it: with the hash of its credential, to
    check what a device presents locally, without asking the hub for every connection."""

    device: SystemDeviceRecord
    credential_hash: str


class DeviceList(Contract):
    system: tuple[SystemDeviceWithCredential, ...] = ()
    external: tuple[ExternalDeviceRecord, ...] = ()


class CreateBindingRequest(Contract):
    skill: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    """The name of a skill (``SkillManifest.name``), not its id: a binding is made before, or
    without, that skill being installed here, and the actions service resolves the name."""

    device_id: DeviceId
    params: dict[str, JsonScalar] = Field(default_factory=dict)
    enabled: bool = True


class BindingView(Contract):
    id: BindingId
    skill: str
    device_id: DeviceId
    params: dict[str, JsonScalar]
    enabled: bool


class BindingList(Contract):
    bindings: tuple[BindingView, ...] = ()
