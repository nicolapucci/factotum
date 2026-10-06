"""The catalog of skills: installing them, reviewing them, and switching them on and off.

Nothing an administrator installs runs until somebody has *reviewed* it and enabled it: enabling a
version grants what it declares (the hosts it may reach, the secrets it may be given, the accounts
it may use) and makes it the version that runs. A new version does not run until it is enabled in
its turn, so what changed is looked at again, and the version that runs stays the one that was
reviewed.

Who may do what follows ownership, and never goes through a device (ADR 0004: through a device one
*uses* the system, one does not administer it). A person manages their own skills; the tenant's are
managed by the tenant's administrators.
"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from factotum.actions.core import CoreSkills
from factotum.actions.matching import SkillIndex, owners_of
from factotum.actions.skills import load_folder, parse_skill
from factotum.actions.store import ActionsStore, StoredSkill, VersionMeta
from factotum.common.authorization import can_access
from factotum.contracts.actions import (
    SkillDetail,
    SkillEnable,
    SkillInfo,
    SkillInstall,
    SkillList,
    SkillOwner,
    SkillReload,
    SkillVersionInfo,
)
from factotum.contracts.errors import ConflictError, PolicyViolationError, SkillNotFoundError
from factotum.contracts.identity import Owner, OwnerKind, Principal, Role, Subject, SubjectKind
from factotum.contracts.ids import SkillId, UserId

NOT_THERE = "there is no such skill"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _person(principal: Principal) -> UserId | None:
    return UserId(principal.acts_as.id) if principal.acts_as.kind is SubjectKind.USER else None


def _info(skill: StoredSkill) -> SkillInfo:
    manifest = skill.latest.manifest
    return SkillInfo(
        id=skill.id,
        tenant_id=skill.tenant_id,
        owner=skill.owner,
        name=skill.name,
        description=manifest.description,
        target=manifest.target,
        mode=manifest.mode,
        effect=manifest.effect,
        latest_version=skill.latest.meta.version,
        enabled_version=None if skill.enabled is None else skill.enabled.meta.version,
    )


def _version_info(meta: VersionMeta, enabled: int | None) -> SkillVersionInfo:
    return SkillVersionInfo(
        version=meta.version,
        content_sha256=meta.content_sha256,
        created_at=meta.created_at,
        enabled=meta.version == enabled,
        enabled_at=meta.enabled_at,
        enabled_by=meta.enabled_by,
    )


def _detail(skill: StoredSkill) -> SkillDetail:
    enabled = None if skill.enabled is None else skill.enabled.meta.version
    return SkillDetail(
        **_info(skill).model_dump(),
        manifest=skill.latest.manifest,
        instructions=skill.latest.instructions,
        versions=tuple(_version_info(meta, enabled) for meta in skill.versions),
        granted=None if skill.enabled is None else skill.enabled.granted,
    )


class Catalog:
    """What is installed, and which version of it runs.

    ``folder`` is a folder of skills (each a folder with a ``SKILL.md`` and its scripts), which is
    the one way to install a skill that has scripts. ``enable_folder`` takes what is read from it to
    be reviewed already and enables it as it is read: for development, where the folder is one's
    own. ``core`` is the skills of the platform (ADR 0026): they are installed for a tenant before
    its administrators first see what is there, as they are before its first message.
    """

    def __init__(
        self,
        store: ActionsStore,
        index: SkillIndex,
        *,
        folder: Path | None = None,
        enable_folder: bool = False,
        core: CoreSkills | None = None,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._store = store
        self._index = index
        self._folder = folder
        self._enable_folder = enable_folder
        self._core = core
        self._now = now

    # ---- who may do what ------------------------------------------------------------------------

    @staticmethod
    def _at_the_hub(principal: Principal) -> None:
        if principal.device_id is not None:
            raise PolicyViolationError("skills are managed from the hub, never through a device")

    @staticmethod
    def _tenants(principal: Principal, owner: Owner) -> None:
        """The skills of the tenant are its administrators'; a person's own are theirs, and nobody
        else can see them to try (``_found``)."""
        if owner.kind is OwnerKind.TENANT and Role.ADMIN not in principal.roles:
            raise PolicyViolationError("the skills of the tenant are managed by its administrators")

    async def _provided(self, principal: Principal) -> None:
        """Have the skills of the platform there for the caller's tenant, once, before what is
        there is shown or added to."""
        if self._core is not None:
            await self._core.ensure(self._store, principal.tenant_id, now=self._now())

    async def _found(self, principal: Principal, skill_id: SkillId) -> StoredSkill:
        """A skill the caller may see, or the same answer as for one that is not there."""
        self._at_the_hub(principal)
        skill = await self._store.get_skill(principal.tenant_id, skill_id)
        if skill is None or not can_access(
            principal, resource_tenant_id=skill.tenant_id, owner=skill.owner
        ):
            raise SkillNotFoundError(NOT_THERE)
        return skill

    async def _managed(self, principal: Principal, skill_id: SkillId) -> StoredSkill:
        skill = await self._found(principal, skill_id)
        self._tenants(principal, skill.owner)
        return skill

    # ---- reading --------------------------------------------------------------------------------

    async def list(self, principal: Principal) -> SkillList:
        """The skills the caller may see: the tenant's and their own."""
        self._at_the_hub(principal)
        await self._provided(principal)
        skills = await self._store.list_skills(principal.tenant_id, owners_of(principal))
        return SkillList(skills=tuple(_info(skill) for skill in skills))

    async def get(self, principal: Principal, skill_id: SkillId) -> SkillDetail:
        return _detail(await self._found(principal, skill_id))

    # ---- installing -----------------------------------------------------------------------------

    async def install(
        self, principal: Principal, request: SkillInstall
    ) -> tuple[SkillDetail, bool]:
        """Install a skill from the text of its ``SKILL.md``. It does not run until it is enabled.
        The second value is whether it was new: the very same skill installed again is not."""
        self._at_the_hub(principal)
        owner = self._owner_of(principal, request.owner)
        self._tenants(principal, owner)
        await self._provided(principal)
        loaded = parse_skill(request.source, where="the skill")
        installed = await self._store.install(principal.tenant_id, owner, loaded, now=self._now())
        stored = await self._store.get_skill(principal.tenant_id, installed.skill_id)
        if stored is None:  # pragma: no cover - it was installed a moment ago
            raise SkillNotFoundError(NOT_THERE)
        return _detail(stored), installed.created

    @staticmethod
    def _owner_of(principal: Principal, whose: SkillOwner) -> Owner:
        if whose is SkillOwner.TENANT:
            return Owner.of_subject(Subject.tenant(principal.tenant_id))
        if principal.acts_as.kind is not SubjectKind.USER:
            raise PolicyViolationError("a skill of one's own is installed by a person")
        return Owner.of_subject(principal.acts_as)

    async def reload(self, principal: Principal) -> SkillReload:
        """Read the folder of skills again and install what is new, as the tenant's. What cannot
        be read is reported, and the rest is installed."""
        self._at_the_hub(principal)
        if self._folder is None:
            raise ConflictError("no folder of skills is set up")
        owner = Owner.of_subject(Subject.tenant(principal.tenant_id))
        self._tenants(principal, owner)
        await self._provided(principal)
        loaded, problems = await asyncio.to_thread(load_folder, self._folder)
        installed: list[str] = []
        enabled: list[str] = []
        unchanged = 0
        for skill in loaded:
            result = await self._store.install(principal.tenant_id, owner, skill, now=self._now())
            label = f"{skill.manifest.name}@{result.version}"
            if result.created:
                installed.append(label)
            else:
                unchanged += 1
            if self._enable_folder and await self._enable(
                principal, result.skill_id, result.version, only_if_not_running=True
            ):
                enabled.append(label)
        return SkillReload(
            installed=tuple(installed),
            enabled=tuple(enabled),
            unchanged=unchanged,
            problems=tuple(problems),
        )

    # ---- reviewing ------------------------------------------------------------------------------

    async def enable(
        self, principal: Principal, skill_id: SkillId, request: SkillEnable
    ) -> SkillDetail:
        """Review a version and make it the one that runs: what it declares is granted."""
        skill = await self._managed(principal, skill_id)
        number = request.version or skill.latest.meta.version
        if not await self._enable(principal, skill_id, number):
            raise SkillNotFoundError("the skill has no such version")
        enabled = await self._store.get_skill(principal.tenant_id, skill_id)
        if enabled is None:  # pragma: no cover - it was there a moment ago
            raise SkillNotFoundError(NOT_THERE)
        return _detail(enabled)

    async def _enable(
        self,
        principal: Principal,
        skill_id: SkillId,
        version: int,
        *,
        only_if_not_running: bool = False,
    ) -> bool:
        """Enable a version with the permissions it declares, and index it. False when there is no
        such version, or when it is the one that runs already and that is what was asked."""
        held = await self._store.get_version(principal.tenant_id, skill_id, version)
        if held is None:
            return False
        if only_if_not_running:
            current = await self._store.get_skill(principal.tenant_id, skill_id)
            if (
                current is not None
                and current.enabled is not None
                and current.enabled.meta.version == version
            ):
                return False
        enabled = await self._store.enable(
            principal.tenant_id,
            skill_id,
            version,
            granted=held.manifest.permissions,
            by=_person(principal),
            now=self._now(),
        )
        if enabled is None:  # pragma: no cover - it was there a moment ago
            return False
        await self._index.index(enabled)
        return True

    async def disable(self, principal: Principal, skill_id: SkillId) -> SkillDetail:
        """Nothing runs of the skill any more. Its versions stay, to be enabled again."""
        await self._managed(principal, skill_id)
        stopped = await self._store.disable(principal.tenant_id, skill_id)
        if stopped is None:  # pragma: no cover - it was there a moment ago
            raise SkillNotFoundError(NOT_THERE)
        return _detail(stopped)

    async def delete(self, principal: Principal, skill_id: SkillId) -> None:
        """The skill, with its versions and its vectors. What was audited about it stays."""
        await self._managed(principal, skill_id)
        await self._store.delete(principal.tenant_id, skill_id)
