"""The device registry: system devices, external devices, their policy and their bindings.

There are two kinds of device. *System* devices are Factotum hardware (a speaker with a
microphone...): they have a hardware id, authenticate with a credential issued at pairing and are
driven through the ready-made ``SystemDevice`` interface. *External* devices (lights, a TV...) are
registered by hand and have no built-in methods: everything they can do is supplied by skills the
user explicitly binds to them.
"""

import re
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from factotum.contracts.base import Contract
from factotum.contracts.capabilities import Capability, DisplayInfo
from factotum.contracts.effects import Effect
from factotum.contracts.identity import Owner, OwnerKind, SubjectKind
from factotum.contracts.ids import BindingId, DeviceId, SkillId, SpaceId, TenantId

_TAG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

JsonScalar = str | int | float | bool


class DeviceKind(StrEnum):
    SYSTEM = "system"
    EXTERNAL = "external"


class DevicePolicy(Contract):
    """Limits on what can be done from, or to, a device.

    ``max_effect`` caps the severity of actions a device may trigger. ``allowed_skills`` restricts
    them to a list (``None`` means no restriction). ``requires_approval`` asks for an out-of-band
    approval on every action.
    """

    max_effect: Effect
    allowed_skills: frozenset[SkillId] | None = None
    requires_approval: bool = False

    @classmethod
    def default_for(cls, kind: SubjectKind) -> Self:
        """A personal device may trigger anything (subject to confirmation); a communal one, which
        anyone can speak to, may not trigger irreversible actions unless its admin allows it."""
        max_effect = Effect.IRREVERSIBLE if kind is SubjectKind.USER else Effect.REVERSIBLE
        return cls(max_effect=max_effect)


class _DeviceBase(Contract):
    id: DeviceId
    tenant_id: TenantId
    name: str = Field(min_length=1, max_length=100)
    aliases: tuple[str, ...] = Field(default=(), max_length=20)
    tags: frozenset[str] = frozenset()
    space_id: SpaceId | None = None
    owner: Owner
    policy: DevicePolicy

    @field_validator("aliases")
    @classmethod
    def _aliases_are_not_blank(cls, aliases: tuple[str, ...]) -> tuple[str, ...]:
        if any(not alias.strip() or len(alias) > 100 for alias in aliases):
            raise ValueError("aliases must be 1 to 100 characters")
        return aliases

    @field_validator("tags")
    @classmethod
    def _tags_are_slugs(cls, tags: frozenset[str]) -> frozenset[str]:
        invalid = sorted(tag for tag in tags if not _TAG.fullmatch(tag))
        if invalid:
            raise ValueError(f"invalid tags {invalid}: use lowercase letters, digits, '_' and '-'")
        return tags

    @field_validator("owner")
    @classmethod
    def _owner_is_a_user_or_the_tenant(cls, owner: Owner) -> Owner:
        if owner.kind is OwnerKind.DEVICE:
            raise ValueError("a device is owned by a user or by the tenant, never by a device")
        return owner


class SystemDeviceRecord(_DeviceBase):
    """A Factotum device. ``hardware_id`` labels the physical unit (it prevents duplicates); it
    is not a proof of identity, which comes from the credential issued at pairing."""

    kind: Literal[DeviceKind.SYSTEM] = DeviceKind.SYSTEM
    hardware_id: str = Field(pattern=r"^[A-Za-z0-9:._-]{1,128}$")
    capabilities: frozenset[Capability]
    display: DisplayInfo | None = None
    protocol_version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _a_display_needs_the_capability(self) -> Self:
        if self.display is not None and Capability.DISPLAY not in self.capabilities:
            raise ValueError("display information requires the 'display' capability")
        return self


class ExternalDeviceRecord(_DeviceBase):
    """An IoT device or anything else Factotum does not run on.

    ``sensitivity`` raises the effect of every action on the device: a generic on/off skill is
    harmless on a lamp and not on a garage door.
    """

    kind: Literal[DeviceKind.EXTERNAL] = DeviceKind.EXTERNAL
    external_refs: dict[str, str] = Field(default_factory=dict)
    sensitivity: Effect = Effect.READ_ONLY


DeviceRecord = Annotated[SystemDeviceRecord | ExternalDeviceRecord, Field(discriminator="kind")]


class Binding(Contract):
    """The association of a skill with a device, with the device's own parameters.

    Skills are reusable: the same skill bound to two lamps differs only in ``params`` (for
    instance the entity id of each lamp).
    """

    id: BindingId
    tenant_id: TenantId
    device_id: DeviceId
    skill_id: SkillId
    params: dict[str, JsonScalar] = Field(default_factory=dict)
    enabled: bool = True
