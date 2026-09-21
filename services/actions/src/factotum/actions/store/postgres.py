"""The store in Postgres, in the schema ``actions``.

What is searched by meaning is small (a few things that each skill says about itself, of the skills
an owner has enabled), so the search is exact, without an index. Every query filters by tenant, and
every transaction says the tenant to the database.
"""

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg

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
from factotum.contracts.identity import Owner, OwnerKind, Subject, SubjectKind
from factotum.contracts.ids import DeviceId, SkillId, TenantId, UserId
from factotum.contracts.skills import SkillManifest, SkillPermissions
from factotum.db.migrations import load_migrations, migrate
from factotum.db.pool import Database
from factotum.db.types import Connection

SCHEMA = "actions"
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"

_NO_DEVICE = UUID(int=0)


def vector_text(vector: Sequence[float]) -> str:
    """A vector the way Postgres reads it as text."""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def vector_sql(dimensions: int) -> str:
    """The search by meaning, exact, among the enabled skills of an owner whose vectors are in a
    given space, the nearest thing each says. Parameters: tenant, owner kind, owner id, space, the
    vector as text, and how many."""
    distance = f"v.embedding::vector({dimensions}) <=> $5::text::vector({dimensions})"
    return (
        "SELECT * FROM ("  # noqa: S608 - the size is an integer
        f"SELECT DISTINCT ON (v.skill_id) v.skill_id, v.version, v.text, 1 - ({distance}) AS score "
        "FROM actions.skill_vectors v JOIN actions.skills s "
        "ON s.id = v.skill_id AND s.enabled_version = v.version "
        "WHERE v.tenant_id = $1 AND s.owner_kind = $2 AND s.owner_id = $3 AND v.space_id = $4 "
        f"ORDER BY v.skill_id, {distance}"
        ") nearest ORDER BY score DESC LIMIT $6"
    )


async def prepare(database: Database) -> list[int]:
    """Bring the tables of the service up to date. Returns the migrations that were applied."""
    async with database.connection() as connection:
        return await migrate(connection, SCHEMA, load_migrations(MIGRATIONS))


def _permissions(text: str | None) -> SkillPermissions | None:
    return None if text is None else SkillPermissions.model_validate_json(text)


def _meta(row: "asyncpg.Record") -> VersionMeta:
    enabled_by = row["enabled_by"]
    return VersionMeta(
        version=row["version"],
        content_sha256=row["content_sha256"],
        created_at=row["created_at"],
        enabled_at=row["enabled_at"],
        enabled_by=None if enabled_by is None else UserId(enabled_by),
    )


def _version(row: "asyncpg.Record") -> StoredVersion:
    return StoredVersion(
        meta=_meta(row),
        manifest=SkillManifest.model_validate_json(row["manifest"]),
        instructions=row["instructions"],
        granted=_permissions(row["granted"]),
    )


def _pending(row: "asyncpg.Record") -> PendingRow:
    return PendingRow(
        id=row["id"],
        tenant_id=TenantId(row["tenant_id"]),
        subject=Subject(kind=SubjectKind(row["subject_kind"]), id=row["subject_id"]),
        device_id=None if row["device_id"] is None else DeviceId(row["device_id"]),
        proposal=json.loads(row["proposal"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


_PENDING = "id, tenant_id, subject_kind, subject_id, device_id, proposal, created_at, expires_at"


class PostgresStore:
    def __init__(self, database: Database) -> None:
        self._db = database

    @property
    def database(self) -> Database:
        return self._db

    # ---- skills
    async def install(
        self, tenant_id: TenantId, owner: Owner, skill: LoadedSkill, *, now: datetime
    ) -> Installed:
        name = skill.manifest.name
        digest = skill.content_sha256
        async with self._db.transaction(tenant_id) as connection:
            # A row is taken (and held until the end of the transaction) even when it is there
            # already, so that two installs of one skill do not both make version 2.
            skill_id = await connection.fetchval(
                "INSERT INTO actions.skills "
                "(id, tenant_id, owner_kind, owner_id, name, created_at) "
                "VALUES ($1, $2, $3, $4, $5, $6) "
                "ON CONFLICT (tenant_id, owner_kind, owner_id, name) "
                "DO UPDATE SET name = excluded.name RETURNING id",
                uuid4(),
                tenant_id,
                owner.kind.value,
                owner.id,
                name,
                now,
            )
            held = await connection.fetchval(
                "SELECT version FROM actions.skill_versions WHERE skill_id = $1 "
                "AND content_sha256 = $2",
                skill_id,
                digest,
            )
            if held is not None:
                return Installed(SkillId(skill_id), held, created=False)
            number = await connection.fetchval(
                "SELECT coalesce(max(version), 0) + 1 FROM actions.skill_versions "
                "WHERE skill_id = $1",
                skill_id,
            )
            await connection.execute(
                "INSERT INTO actions.skill_versions (skill_id, version, tenant_id, content_sha256, "
                "created_at, manifest, instructions) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)",
                skill_id,
                number,
                tenant_id,
                digest,
                now,
                skill.manifest.model_dump_json(),
                skill.instructions,
            )
            if skill.files:
                await connection.executemany(
                    "INSERT INTO actions.skill_files (skill_id, version, path, content) "
                    "VALUES ($1, $2, $3, $4)",
                    [(skill_id, number, path, content) for path, content in skill.files.items()],
                )
        return Installed(SkillId(skill_id), number, created=True)

    async def list_skills(
        self, tenant_id: TenantId, owners: Sequence[Owner], *, enabled_only: bool = False
    ) -> list[StoredSkill]:
        if not owners:
            return []
        async with self._db.transaction(tenant_id) as connection:
            rows = await connection.fetch(
                "SELECT s.* FROM actions.skills s "
                "JOIN unnest($2::text[], $3::uuid[]) AS o(kind, id) "
                "ON s.owner_kind = o.kind AND s.owner_id = o.id "
                "WHERE s.tenant_id = $1 AND ($4 = false OR s.enabled_version IS NOT NULL) "
                "ORDER BY s.name",
                tenant_id,
                [owner.kind.value for owner in owners],
                [owner.id for owner in owners],
                enabled_only,
            )
            return await self._assemble(connection, rows)

    async def get_skill(self, tenant_id: TenantId, skill_id: SkillId) -> StoredSkill | None:
        async with self._db.transaction(tenant_id) as connection:
            rows = await connection.fetch(
                "SELECT * FROM actions.skills WHERE tenant_id = $1 AND id = $2",
                tenant_id,
                skill_id,
            )
            found = await self._assemble(connection, rows)
        return found[0] if found else None

    @staticmethod
    async def _assemble(
        connection: Connection, skills: Sequence["asyncpg.Record"]
    ) -> list[StoredSkill]:
        if not skills:
            return []
        versions = await connection.fetch(
            "SELECT skill_id, version, content_sha256, created_at, manifest, instructions, "
            "granted, enabled_at, enabled_by FROM actions.skill_versions "
            "WHERE skill_id = ANY($1::uuid[]) ORDER BY skill_id, version",
            [row["id"] for row in skills],
        )
        by_skill: dict[UUID, list[asyncpg.Record]] = {}
        for version in versions:
            by_skill.setdefault(version["skill_id"], []).append(version)
        stored: list[StoredSkill] = []
        for row in skills:
            held = by_skill.get(row["id"], [])
            if not held:  # pragma: no cover - a skill is made together with its first version
                continue
            enabled = row["enabled_version"]
            stored.append(
                StoredSkill(
                    id=SkillId(row["id"]),
                    tenant_id=TenantId(row["tenant_id"]),
                    owner=Owner(kind=OwnerKind(row["owner_kind"]), id=row["owner_id"]),
                    name=row["name"],
                    created_at=row["created_at"],
                    versions=tuple(_meta(v) for v in held),
                    latest=_version(held[-1]),
                    enabled=next((_version(v) for v in held if v["version"] == enabled), None),
                )
            )
        return stored

    async def get_version(
        self, tenant_id: TenantId, skill_id: SkillId, version: int
    ) -> StoredVersion | None:
        async with self._db.transaction(tenant_id) as connection:
            row = await connection.fetchrow(
                "SELECT version, content_sha256, created_at, manifest, instructions, granted, "
                "enabled_at, enabled_by FROM actions.skill_versions "
                "WHERE tenant_id = $1 AND skill_id = $2 AND version = $3",
                tenant_id,
                skill_id,
                version,
            )
        return None if row is None else _version(row)

    async def get_files(
        self, tenant_id: TenantId, skill_id: SkillId, version: int
    ) -> dict[str, bytes] | None:
        async with self._db.transaction(tenant_id) as connection:
            exists = await connection.fetchval(
                "SELECT 1 FROM actions.skill_versions WHERE tenant_id = $1 AND skill_id = $2 "
                "AND version = $3",
                tenant_id,
                skill_id,
                version,
            )
            if exists is None:
                return None
            rows = await connection.fetch(
                "SELECT path, content FROM actions.skill_files "
                "WHERE skill_id = $1 AND version = $2",
                skill_id,
                version,
            )
        return {row["path"]: bytes(row["content"]) for row in rows}

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
        async with self._db.transaction(tenant_id) as connection:
            changed = await connection.fetchval(
                "UPDATE actions.skill_versions SET granted = $4::jsonb, enabled_at = $5, "
                "enabled_by = $6 WHERE tenant_id = $1 AND skill_id = $2 AND version = $3 "
                "RETURNING version",
                tenant_id,
                skill_id,
                version,
                granted.model_dump_json(),
                now,
                by,
            )
            if changed is None:
                return None
            await connection.execute(
                "UPDATE actions.skills SET enabled_version = $3 WHERE tenant_id = $1 AND id = $2",
                tenant_id,
                skill_id,
                version,
            )
        return await self.get_skill(tenant_id, skill_id)

    async def disable(self, tenant_id: TenantId, skill_id: SkillId) -> StoredSkill | None:
        async with self._db.transaction(tenant_id) as connection:
            changed = await connection.fetchval(
                "UPDATE actions.skills SET enabled_version = NULL WHERE tenant_id = $1 AND id = $2 "
                "RETURNING id",
                tenant_id,
                skill_id,
            )
        return None if changed is None else await self.get_skill(tenant_id, skill_id)

    async def delete(self, tenant_id: TenantId, skill_id: SkillId) -> bool:
        async with self._db.transaction(tenant_id) as connection:
            status = await connection.execute(
                "DELETE FROM actions.skills WHERE tenant_id = $1 AND id = $2", tenant_id, skill_id
            )
        return status.endswith(" 1")

    # ---- vectors
    async def put_vectors(
        self,
        tenant_id: TenantId,
        skill_id: SkillId,
        version: int,
        space_id: str,
        rows: Sequence[VectorRow],
    ) -> None:
        async with self._db.transaction(tenant_id) as connection:
            exists = await connection.fetchval(
                "SELECT 1 FROM actions.skill_versions WHERE tenant_id = $1 AND skill_id = $2 "
                "AND version = $3",
                tenant_id,
                skill_id,
                version,
            )
            if exists is None:
                return
            await connection.execute(
                "DELETE FROM actions.skill_vectors WHERE skill_id = $1 AND version = $2",
                skill_id,
                version,
            )
            await connection.executemany(
                "INSERT INTO actions.skill_vectors (skill_id, version, tenant_id, space_id, seq, "
                "text, embedding) VALUES ($1, $2, $3, $4, $5, $6, $7::text::vector)",
                [
                    (skill_id, version, tenant_id, space_id, seq, row.text, vector_text(row.vector))
                    for seq, row in enumerate(rows)
                ],
            )

    async def indexed(self, tenant_id: TenantId, owner: Owner, space_id: str) -> set[SkillId]:
        async with self._db.transaction(tenant_id) as connection:
            rows = await connection.fetch(
                "SELECT DISTINCT s.id FROM actions.skills s JOIN actions.skill_vectors v "
                "ON v.skill_id = s.id AND v.version = s.enabled_version "
                "WHERE s.tenant_id = $1 AND s.owner_kind = $2 AND s.owner_id = $3 "
                "AND v.space_id = $4",
                tenant_id,
                owner.kind.value,
                owner.id,
                space_id,
            )
        return {SkillId(row["id"]) for row in rows}

    async def search_vectors(
        self,
        tenant_id: TenantId,
        owner: Owner,
        space_id: str,
        vector: Sequence[float],
        *,
        limit: int,
    ) -> list[SkillMatch]:
        async with self._db.transaction(tenant_id) as connection:
            rows = await connection.fetch(
                vector_sql(len(vector)),
                tenant_id,
                owner.kind.value,
                owner.id,
                space_id,
                vector_text(vector),
                limit,
            )
        return [
            SkillMatch(SkillId(row["skill_id"]), row["version"], float(row["score"]), row["text"])
            for row in rows
        ]

    async def forget_other_spaces(self, tenant_id: TenantId, owner: Owner, space_id: str) -> int:
        async with self._db.transaction(tenant_id) as connection:
            status = await connection.execute(
                "DELETE FROM actions.skill_vectors v USING actions.skills s "
                "WHERE v.skill_id = s.id AND s.tenant_id = $1 AND s.owner_kind = $2 "
                "AND s.owner_id = $3 AND v.space_id <> $4",
                tenant_id,
                owner.kind.value,
                owner.id,
                space_id,
            )
        return int(status.rsplit(" ", 1)[-1])

    # ---- confirmations
    async def put_pending(self, pending: PendingRow) -> None:
        async with self._db.transaction(pending.tenant_id) as connection:
            await connection.execute(
                "DELETE FROM actions.confirmations WHERE tenant_id = $1 AND subject_kind = $2 "
                "AND subject_id = $3 AND coalesce(device_id, $4) = $5",
                pending.tenant_id,
                pending.subject.kind.value,
                pending.subject.id,
                _NO_DEVICE,
                pending.device_id or _NO_DEVICE,
            )
            await connection.execute(
                f"INSERT INTO actions.confirmations ({_PENDING}) "  # noqa: S608 - a constant
                "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)",
                pending.id,
                pending.tenant_id,
                pending.subject.kind.value,
                pending.subject.id,
                pending.device_id,
                json.dumps(pending.proposal),
                pending.created_at,
                pending.expires_at,
            )

    async def get_pending(
        self, tenant_id: TenantId, subject: Subject, device_id: DeviceId | None
    ) -> PendingRow | None:
        async with self._db.transaction(tenant_id) as connection:
            row = await connection.fetchrow(
                f"SELECT {_PENDING} FROM actions.confirmations "  # noqa: S608 - a constant
                "WHERE tenant_id = $1 AND subject_kind = $2 AND subject_id = $3 "
                "AND coalesce(device_id, $4) = $5",
                tenant_id,
                subject.kind.value,
                subject.id,
                _NO_DEVICE,
                device_id or _NO_DEVICE,
            )
        return None if row is None else _pending(row)

    async def take_pending(self, tenant_id: TenantId, pending_id: UUID) -> PendingRow | None:
        async with self._db.transaction(tenant_id) as connection:
            row = await connection.fetchrow(
                "DELETE FROM actions.confirmations WHERE tenant_id = $1 AND id = $2 RETURNING "  # noqa: S608
                + _PENDING,
                tenant_id,
                pending_id,
            )
        return None if row is None else _pending(row)

    # ---- audit
    async def add_audit(self, record: AuditRecord) -> None:
        async with self._db.transaction(record.tenant_id) as connection:
            await connection.execute(
                "INSERT INTO actions.audit (id, tenant_id, occurred_at, skill_name, device_id, "
                "record) VALUES ($1, $2, $3, $4, $5, $6::jsonb)",
                record.id,
                record.tenant_id,
                record.occurred_at,
                record.skill_name,
                record.device_id,
                record.model_dump_json(),
            )

    async def list_audit(
        self,
        tenant_id: TenantId,
        *,
        before: datetime | None,
        limit: int,
        skill_name: str | None = None,
        device_id: DeviceId | None = None,
    ) -> list[AuditRecord]:
        async with self._db.transaction(tenant_id) as connection:
            rows = await connection.fetch(
                "SELECT record FROM actions.audit WHERE tenant_id = $1 "
                "AND ($2::timestamptz IS NULL OR occurred_at < $2) "
                "AND ($3::text IS NULL OR skill_name = $3) "
                "AND ($4::uuid IS NULL OR device_id = $4) "
                "ORDER BY occurred_at DESC, id DESC LIMIT $5",
                tenant_id,
                before,
                skill_name,
                device_id,
                limit,
            )
        return [AuditRecord.model_validate_json(row["record"]) for row in rows]
