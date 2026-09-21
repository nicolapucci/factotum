"""What the actions service keeps, and the interface it is kept behind.

Four things: the skills that are installed (each in immutable versions, and one version the one that
runs, once an administrator has reviewed it), the vectors of what they say about themselves (to
find the ones a message may be asking for), the confirmations that are waiting for a yes or a no,
and the audit trail, which is only ever added to. Every method takes the tenant, and every query
filters by it: nothing of one tenant is ever seen through another.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from factotum.actions.skills import LoadedSkill
from factotum.contracts.audit import AuditRecord
from factotum.contracts.identity import Owner, Subject
from factotum.contracts.ids import DeviceId, SkillId, TenantId, UserId
from factotum.contracts.skills import SkillManifest, SkillPermissions


@dataclass(frozen=True, slots=True)
class VersionMeta:
    version: int
    content_sha256: str
    created_at: datetime
    enabled_at: datetime | None = None
    enabled_by: UserId | None = None
    """When and by whom this version was last reviewed and enabled (it may not be the one that
    runs now)."""


@dataclass(frozen=True, slots=True)
class StoredVersion:
    meta: VersionMeta
    manifest: SkillManifest
    instructions: str
    granted: SkillPermissions | None = None
    """What was granted when it was enabled: the most it may reach."""


@dataclass(frozen=True, slots=True)
class StoredSkill:
    id: SkillId
    tenant_id: TenantId
    owner: Owner
    name: str
    created_at: datetime
    versions: tuple[VersionMeta, ...]
    latest: StoredVersion
    enabled: StoredVersion | None = None
    """The version that runs, or nothing when no version has been enabled."""


@dataclass(frozen=True, slots=True)
class Installed:
    skill_id: SkillId
    version: int
    created: bool
    """False when the very same skill was already installed: nothing was added."""


@dataclass(frozen=True, slots=True)
class VectorRow:
    """What a skill says about itself, and where that is in meaning."""

    text: str
    vector: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class SkillMatch:
    skill_id: SkillId
    version: int
    score: float
    """How near the message is to the nearest thing the skill says about itself (1 is the same)."""

    matched: str
    """That thing."""


@dataclass(frozen=True, slots=True)
class PendingRow:
    """A confirmation that is waiting for a yes or a no. What is confirmed is ``proposal``, kept
    as the service wrote it: the store does not read it."""

    id: UUID
    tenant_id: TenantId
    subject: Subject
    device_id: DeviceId | None
    proposal: dict[str, Any]
    created_at: datetime
    expires_at: datetime


class ActionsStore(Protocol):
    # ---- skills
    async def install(
        self, tenant_id: TenantId, owner: Owner, skill: LoadedSkill, *, now: datetime
    ) -> Installed:
        """Keep a skill as a new version of the one of this name and owner (the first one, when
        there is none). The very same content is not a new version."""
        ...

    async def list_skills(
        self, tenant_id: TenantId, owners: Sequence[Owner], *, enabled_only: bool = False
    ) -> list[StoredSkill]:
        """The skills of these owners, by name; with ``enabled_only``, those that have a version
        that runs."""
        ...

    async def get_skill(self, tenant_id: TenantId, skill_id: SkillId) -> StoredSkill | None: ...

    async def get_version(
        self, tenant_id: TenantId, skill_id: SkillId, version: int
    ) -> StoredVersion | None:
        """One version of a skill, whichever it is: the latest, the one that runs, an older one."""
        ...

    async def get_files(
        self, tenant_id: TenantId, skill_id: SkillId, version: int
    ) -> dict[str, bytes] | None:
        """The files of a version beside its ``SKILL.md``, or nothing when there is no such
        version."""
        ...

    async def enable(
        self,
        tenant_id: TenantId,
        skill_id: SkillId,
        version: int,
        *,
        granted: SkillPermissions,
        by: UserId | None,
        now: datetime,
    ) -> StoredSkill | None:
        """Make a version the one that runs, with what it was granted. Nothing when there is no such
        skill or version."""
        ...

    async def disable(self, tenant_id: TenantId, skill_id: SkillId) -> StoredSkill | None:
        """Nothing runs of the skill any more. Its versions stay."""
        ...

    async def delete(self, tenant_id: TenantId, skill_id: SkillId) -> bool:
        """The skill, its versions and its vectors. What was audited stays."""
        ...

    # ---- what skills say about themselves, by meaning
    async def put_vectors(
        self,
        tenant_id: TenantId,
        skill_id: SkillId,
        version: int,
        space_id: str,
        rows: Sequence[VectorRow],
    ) -> None:
        """The vectors of a version, replacing any it had."""
        ...

    async def indexed(self, tenant_id: TenantId, owner: Owner, space_id: str) -> set[SkillId]:
        """The enabled skills of an owner whose enabled version has vectors in this space."""
        ...

    async def search_vectors(
        self,
        tenant_id: TenantId,
        owner: Owner,
        space_id: str,
        vector: Sequence[float],
        *,
        limit: int,
    ) -> list[SkillMatch]:
        """The enabled skills of an owner nearest to a vector, one match each (the nearest thing it
        says), the nearest first. Only vectors of this space are compared."""
        ...

    async def forget_other_spaces(self, tenant_id: TenantId, owner: Owner, space_id: str) -> int:
        """Drop the vectors of the owner's skills that are not in this space: the embedding model
        behind the owner's role is another one now. Returns how many were dropped."""
        ...

    # ---- confirmations
    async def put_pending(self, pending: PendingRow) -> None:
        """Wait for a yes or a no. There is one at a time for a person on a device: a new one
        replaces the one that was there."""
        ...

    async def get_pending(
        self, tenant_id: TenantId, subject: Subject, device_id: DeviceId | None
    ) -> PendingRow | None:
        """What is waiting for this person on this device, whether or not it has expired."""
        ...

    async def take_pending(self, tenant_id: TenantId, pending_id: UUID) -> PendingRow | None:
        """Take what was waiting: it is given to one caller only, however many ask (an answer that
        arrives twice does the thing once). Nothing when it was taken already."""
        ...

    # ---- audit
    async def add_audit(self, record: AuditRecord) -> None: ...

    async def list_audit(
        self,
        tenant_id: TenantId,
        *,
        before: datetime | None,
        limit: int,
        skill_name: str | None = None,
        device_id: DeviceId | None = None,
    ) -> list[AuditRecord]:
        """The latest first."""
        ...
