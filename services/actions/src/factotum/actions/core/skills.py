"""The skills that the platform provides (ADR 0026): written in code, run in the process of the
actions service, and found, chosen, authorized, confirmed and audited as any skill is.

A core skill has a ``SKILL.md`` like the others, which is what the matcher and the planner read, and
a function that runs it. What makes a stored version *the* core skill is not anything that a
manifest says (anybody can write a manifest): it is that the hash of its content is the hash of the
text that the platform carries. A person's own skill with another text is a skill of theirs, run in
the sandbox as every skill is, and the very same text is the platform's skill and nothing is gained
by it.

The first time a tenant is asked for, the skills of the platform are installed for it and enabled,
and left alone after that: one that an administrator disabled stays disabled until a new version of
it ships.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Protocol

from factotum.actions.skills import LoadedSkill, parse_skill
from factotum.actions.store import ActionsStore, StoredSkill
from factotum.contracts.audio import AudioPayload
from factotum.contracts.commands import CommandResult, DeviceCommand
from factotum.contracts.identity import Owner, Principal, Subject
from factotum.contracts.ids import TenantId
from factotum.contracts.runner import RunRequest
from factotum.contracts.skills import SkillManifest

logger = logging.getLogger("factotum.actions")


class SpeechMaker(Protocol):
    """What a core skill that has something to say asks for: the voice of a text, as a clip."""

    async def synthesize(self, owner: Owner, text: str) -> AudioPayload: ...


@dataclass(frozen=True, slots=True)
class CoreResult:
    """What a core skill did: what to say of it, what a device is to do, and what the trail says."""

    message: str | None = None
    """What to say to the person who asked. Nothing is the usual word for a thing that was done."""

    commands: tuple[DeviceCommand, ...] = ()
    """What the platform is to have the device do, once the checks are made: the whole vocabulary,
    ``play_audio`` among it (a skill that is not the platform's cannot give that one)."""

    audit: str | None = None
    """What the audit trail says was done, for a person to read: a clip is opaque, a text is not."""

    after: Callable[[Sequence[CommandResult]], str | None] | None = None
    """What to say once the device has done the commands, when what there is to say is what the
    device answered (the level the volume came to, how it is; ADR 0029). It is given what each
    command answered, in order, and the words it returns replace ``message``; nothing returned is
    the usual word for a thing that was done. It raises ``SkillRunError`` for an answer that is not
    one, and the skill has failed. What the device answered is read here, in code: a model never
    reads it."""


class CoreSkill(ABC):
    """A skill of the platform. It is called with what a message said (``request.params``), and
    later with what other skills say: so what it is given is checked as the input that it is."""

    source: ClassVar[str]
    """The text of its ``SKILL.md``, which is what a stored version has to be to be this skill."""

    def __init__(self) -> None:
        self.loaded: LoadedSkill = parse_skill(
            self.source, where=f"the core skill {type(self).__name__}"
        )
        self.name: str = self.loaded.manifest.name

    @property
    def content_sha256(self) -> str:
        return self.loaded.content_sha256

    @abstractmethod
    async def run(self, request: RunRequest, principal: Principal, *, language: str) -> CoreResult:
        """Do what the skill is for, for the principal that asked, who spoke ``language`` (what it
        says of what it did is in it). Raises ``SkillRunError`` for what it cannot do, in words that
        a person may read: when a service it asks for fails it says what kind of failure it was (the
        code of the error) and never what the service said."""


class CoreSkills:
    """The skills of the platform that a service is given, and what it needs of them."""

    def __init__(self, *skills: CoreSkill) -> None:
        names = [skill.name for skill in skills]
        if len(set(names)) != len(names):
            raise ValueError("a core skill is there once")
        self._skills = skills
        self._by_name = {skill.name: skill for skill in skills}
        self._ready: set[TenantId] = set()
        self._lock = asyncio.Lock()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._by_name)

    def manifests(self) -> dict[str, SkillManifest]:
        """What each says about itself, by name: what a message is matched against and a model is
        shown, whoever reads it."""
        return {name: skill.loaded.manifest for name, skill in self._by_name.items()}

    def recognize(self, skill: StoredSkill) -> CoreSkill | None:
        """The core skill that the version of ``skill`` that runs is, or nothing when it is not one:
        its name and the hash of its content are those of the platform's."""
        enabled = skill.enabled
        core = self._by_name.get(skill.name)
        if enabled is None or core is None:
            return None
        return core if enabled.meta.content_sha256 == core.content_sha256 else None

    async def ensure(
        self, store: ActionsStore, tenant_id: TenantId, *, now: datetime
    ) -> tuple[StoredSkill, ...]:
        """Have the skills of the platform installed for a tenant, and enabled when nobody ever
        enabled that version of them: a first time, a version that the platform did not carry
        before, or one that was installed and never enabled because the service stopped in between.
        A version that was enabled and then disabled stays disabled. Done once for a tenant for as
        long as the service runs; the skills that were enabled now, which whoever asked is to have
        indexed by meaning (a skill that is not is offered to a model unscored, for every message
        until it is)."""
        if tenant_id in self._ready:
            return ()
        enabled_now: list[StoredSkill] = []
        async with self._lock:
            if tenant_id in self._ready:
                return ()
            owner = Owner.of_subject(Subject.tenant(tenant_id))
            for core in self._skills:
                installed = await store.install(tenant_id, owner, core.loaded, now=now)
                held = await store.get_version(tenant_id, installed.skill_id, installed.version)
                if held is not None and held.meta.enabled_at is None:
                    enabled = await store.enable(
                        tenant_id,
                        installed.skill_id,
                        installed.version,
                        granted=core.loaded.manifest.permissions,
                        by=None,
                        now=now,
                    )
                    if enabled is not None:
                        enabled_now.append(enabled)
            self._ready.add(tenant_id)
        if enabled_now:
            logger.info(
                "the skills of the platform were enabled",
                extra={"skills": [f"{skill.name}" for skill in enabled_now]},
            )
        return tuple(enabled_now)
