"""The store in memory: what the tests of everything above it use, and the reference for what the
Postgres store has to do (the same tests run against both)."""

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID, uuid4

from factotum.actions.skills import LoadedSkill
from factotum.actions.store.base import (
    Installed,
    PendingRow,
    SkillMatch,
    StoredSkill,
    StoredVersion,
    VectorRow,
    VersionMeta,
)
from factotum.contracts.audit import AuditRecord
from factotum.contracts.identity import Owner, Subject
from factotum.contracts.ids import DeviceId, SkillId, TenantId, UserId
from factotum.contracts.skills import SkillManifest, SkillPermissions


@dataclass
class _Version:
    meta: VersionMeta
    manifest: SkillManifest
    instructions: str
    files: dict[str, bytes]
    granted: SkillPermissions | None = None


@dataclass
class _Skill:
    id: SkillId
    tenant_id: TenantId
    owner: Owner
    name: str
    created_at: datetime
    versions: list[_Version]
    enabled: int | None = None


def _cosine(one: Sequence[float], other: Sequence[float]) -> float:
    if len(one) != len(other):
        return 0.0
    norms = math.sqrt(sum(x * x for x in one)) * math.sqrt(sum(y * y for y in other))
    if norms == 0:
        return 0.0
    return sum(x * y for x, y in zip(one, other, strict=True)) / norms


class InMemoryStore:
    def __init__(self) -> None:
        self._skills: dict[SkillId, _Skill] = {}
        self._vectors: dict[tuple[SkillId, int], tuple[str, tuple[VectorRow, ...]]] = {}
        self._pending: dict[tuple[TenantId, Subject, DeviceId | None], PendingRow] = {}
        self._audit: list[AuditRecord] = []

    # ---- skills
    async def install(
        self, tenant_id: TenantId, owner: Owner, skill: LoadedSkill, *, now: datetime
    ) -> Installed:
        name = skill.manifest.name
        existing = next(
            (
                s
                for s in self._skills.values()
                if (s.tenant_id, s.owner, s.name) == (tenant_id, owner, name)
            ),
            None,
        )
        digest = skill.content_sha256
        if existing is None:
            existing = _Skill(SkillId(uuid4()), tenant_id, owner, name, now, [])
            self._skills[existing.id] = existing
        else:
            for held in existing.versions:
                if held.meta.content_sha256 == digest:
                    return Installed(existing.id, held.meta.version, created=False)
        number = len(existing.versions) + 1
        existing.versions.append(
            _Version(
                meta=VersionMeta(version=number, content_sha256=digest, created_at=now),
                manifest=skill.manifest,
                instructions=skill.instructions,
                files=dict(skill.files),
            )
        )
        return Installed(existing.id, number, created=True)

    async def list_skills(
        self, tenant_id: TenantId, owners: Sequence[Owner], *, enabled_only: bool = False
    ) -> list[StoredSkill]:
        found = [
            self._stored(s)
            for s in self._skills.values()
            if s.tenant_id == tenant_id
            and s.owner in owners
            and (s.enabled is not None or not enabled_only)
        ]
        return sorted(found, key=lambda s: s.name)

    async def get_skill(self, tenant_id: TenantId, skill_id: SkillId) -> StoredSkill | None:
        skill = self._mine(tenant_id, skill_id)
        return None if skill is None else self._stored(skill)

    async def get_version(
        self, tenant_id: TenantId, skill_id: SkillId, version: int
    ) -> StoredVersion | None:
        skill = self._mine(tenant_id, skill_id)
        held = None if skill is None else self._version(skill, version)
        return None if held is None else self._stored_version(held)

    async def get_files(
        self, tenant_id: TenantId, skill_id: SkillId, version: int
    ) -> dict[str, bytes] | None:
        skill = self._mine(tenant_id, skill_id)
        held = None if skill is None else self._version(skill, version)
        return None if held is None else dict(held.files)

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
        skill = self._mine(tenant_id, skill_id)
        held = None if skill is None else self._version(skill, version)
        if skill is None or held is None:
            return None
        held.meta = replace(held.meta, enabled_at=now, enabled_by=by)
        held.granted = granted
        skill.enabled = version
        return self._stored(skill)

    async def disable(self, tenant_id: TenantId, skill_id: SkillId) -> StoredSkill | None:
        skill = self._mine(tenant_id, skill_id)
        if skill is None:
            return None
        skill.enabled = None
        return self._stored(skill)

    async def delete(self, tenant_id: TenantId, skill_id: SkillId) -> bool:
        skill = self._mine(tenant_id, skill_id)
        if skill is None:
            return False
        del self._skills[skill_id]
        for key in [key for key in self._vectors if key[0] == skill_id]:
            del self._vectors[key]
        return True

    # ---- vectors
    async def put_vectors(
        self,
        tenant_id: TenantId,
        skill_id: SkillId,
        version: int,
        space_id: str,
        rows: Sequence[VectorRow],
    ) -> None:
        skill = self._mine(tenant_id, skill_id)
        if skill is not None and self._version(skill, version) is not None:
            self._vectors[(skill_id, version)] = (space_id, tuple(rows))

    def _enabled_vectors(
        self, tenant_id: TenantId, owner: Owner, space_id: str
    ) -> list[tuple[_Skill, tuple[VectorRow, ...]]]:
        found: list[tuple[_Skill, tuple[VectorRow, ...]]] = []
        for skill in self._skills.values():
            if skill.tenant_id != tenant_id or skill.owner != owner or skill.enabled is None:
                continue
            held = self._vectors.get((skill.id, skill.enabled))
            if held is not None and held[0] == space_id:
                found.append((skill, held[1]))
        return found

    async def indexed(self, tenant_id: TenantId, owner: Owner, space_id: str) -> set[SkillId]:
        return {skill.id for skill, _ in self._enabled_vectors(tenant_id, owner, space_id)}

    async def search_vectors(
        self,
        tenant_id: TenantId,
        owner: Owner,
        space_id: str,
        vector: Sequence[float],
        *,
        limit: int,
    ) -> list[SkillMatch]:
        matches: list[SkillMatch] = []
        for skill, rows in self._enabled_vectors(tenant_id, owner, space_id):
            scored = [(_cosine(vector, row.vector), row.text) for row in rows]
            if scored:
                score, text = max(scored)
                matches.append(SkillMatch(skill.id, skill.enabled or 0, score, text))
        matches.sort(key=lambda m: m.score, reverse=True)
        return matches[:limit]

    async def forget_other_spaces(self, tenant_id: TenantId, owner: Owner, space_id: str) -> int:
        stale = [
            key
            for key, (space, _) in self._vectors.items()
            if space != space_id
            and (skill := self._skills.get(key[0])) is not None
            and skill.tenant_id == tenant_id
            and skill.owner == owner
        ]
        for key in stale:
            del self._vectors[key]
        return len(stale)

    # ---- confirmations
    async def put_pending(self, pending: PendingRow) -> None:
        self._pending[(pending.tenant_id, pending.subject, pending.device_id)] = pending

    async def get_pending(
        self, tenant_id: TenantId, subject: Subject, device_id: DeviceId | None
    ) -> PendingRow | None:
        return self._pending.get((tenant_id, subject, device_id))

    async def take_pending(self, tenant_id: TenantId, pending_id: UUID) -> PendingRow | None:
        for key, row in self._pending.items():
            if row.id == pending_id and row.tenant_id == tenant_id:
                del self._pending[key]
                return row
        return None

    # ---- audit
    async def add_audit(self, record: AuditRecord) -> None:
        self._audit.append(record)

    async def list_audit(
        self,
        tenant_id: TenantId,
        *,
        before: datetime | None,
        limit: int,
        skill_name: str | None = None,
        device_id: DeviceId | None = None,
    ) -> list[AuditRecord]:
        found = [
            record
            for record in self._audit
            if record.tenant_id == tenant_id
            and (before is None or record.occurred_at < before)
            and (skill_name is None or record.skill_name == skill_name)
            and (device_id is None or record.device_id == device_id)
        ]
        found.sort(key=lambda record: (record.occurred_at, record.id), reverse=True)
        return found[:limit]

    # ---- helpers
    def _mine(self, tenant_id: TenantId, skill_id: SkillId) -> _Skill | None:
        skill = self._skills.get(skill_id)
        return skill if skill is not None and skill.tenant_id == tenant_id else None

    @staticmethod
    def _version(skill: _Skill, number: int) -> _Version | None:
        return next((v for v in skill.versions if v.meta.version == number), None)

    @staticmethod
    def _stored_version(held: _Version) -> StoredVersion:
        return StoredVersion(
            meta=held.meta,
            manifest=held.manifest,
            instructions=held.instructions,
            granted=held.granted,
        )

    def _stored(self, skill: _Skill) -> StoredSkill:
        enabled = None if skill.enabled is None else self._version(skill, skill.enabled)
        return StoredSkill(
            id=skill.id,
            tenant_id=skill.tenant_id,
            owner=skill.owner,
            name=skill.name,
            created_at=skill.created_at,
            versions=tuple(v.meta for v in skill.versions),
            latest=self._stored_version(skill.versions[-1]),
            enabled=None if enabled is None else self._stored_version(enabled),
        )
