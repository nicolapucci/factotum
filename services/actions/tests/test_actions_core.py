"""The skills that the platform provides (ADR 0026): installed for a tenant when it is first asked
for, enabled when nobody ever enabled that version, found by the hash of their text, and never
mistaken for a skill that somebody wrote."""

import asyncio
import dataclasses
from collections.abc import Sequence
from datetime import datetime
from typing import ClassVar

import pytest

from factotum.actions.core import Announce, CoreResult, CoreSkill, CoreSkills
from factotum.actions.skills import LoadedSkill, parse_skill
from factotum.actions.store import ActionsStore, InMemoryStore, Installed, StoredSkill
from factotum.actions.testing import START, FakeSpeech, just_announce, stored_skill
from factotum.contracts.effects import Effect
from factotum.contracts.errors import SkillInvalidError
from factotum.contracts.identity import Owner, Principal, Subject
from factotum.contracts.ids import TenantId
from factotum.contracts.runner import RunRequest
from factotum.contracts.skills import SkillPermissions, SkillTarget
from factotum.contracts.testing import make_tenant_id, make_user_id


class Newer(CoreSkill):
    """A later version of the platform's skill: the same name, and another text."""

    source: ClassVar[str] = Announce.source.replace("effect: reversible", "effect: irreversible")

    async def run(self, request: RunRequest, principal: Principal, *, language: str) -> CoreResult:
        return CoreResult(message="newer")


class CountingStore(InMemoryStore):
    """A store that counts how many times a skill was installed in it."""

    def __init__(self) -> None:
        super().__init__()
        self.installs = 0

    async def install(
        self, tenant_id: TenantId, owner: Owner, skill: LoadedSkill, *, now: datetime
    ) -> Installed:
        self.installs += 1
        return await super().install(tenant_id, owner, skill, now=now)


def owner_of(tenant: TenantId) -> Owner:
    return Owner.of_subject(Subject.tenant(tenant))


async def skills_of(
    store: ActionsStore, tenant: TenantId, *, enabled_only: bool = True
) -> list[StoredSkill]:
    return await store.list_skills(tenant, [owner_of(tenant)], enabled_only=enabled_only)


def labels(skills: Sequence[StoredSkill]) -> list[str]:
    """What was enabled, as name and version."""
    found: list[str] = []
    for skill in skills:
        assert skill.enabled is not None
        found.append(f"{skill.name}@{skill.enabled.meta.version}")
    return found


class TestHavingThemInstalled:
    async def test_a_tenant_that_is_asked_for_has_them_installed_and_enabled(self) -> None:
        store, tenant = InMemoryStore(), make_tenant_id()
        core = just_announce()

        enabled = await core.ensure(store, tenant, now=START)

        assert labels(enabled) == ["announce@1"]
        [skill] = await skills_of(store, tenant)
        assert skill.name == "announce"
        assert skill.enabled is not None
        assert skill.enabled.granted == skill.enabled.manifest.permissions
        assert skill.enabled.meta.enabled_by is None  # the platform did it, and no person
        assert core.recognize(skill) is not None

    async def test_what_is_returned_is_the_skills_as_they_run_not_their_names(self) -> None:
        store, tenant = InMemoryStore(), make_tenant_id()

        [skill] = await just_announce().ensure(store, tenant, now=START)

        assert (skill.name, skill.tenant_id, skill.owner) == ("announce", tenant, owner_of(tenant))
        assert skill.enabled is not None
        assert skill.enabled.meta.version == 1

    async def test_it_is_done_once_for_a_tenant_for_as_long_as_the_service_runs(self) -> None:
        store, tenant = InMemoryStore(), make_tenant_id()
        core = just_announce()
        await core.ensure(store, tenant, now=START)

        again = await core.ensure(store, tenant, now=START)

        assert again == ()
        [skill] = await skills_of(store, tenant)
        assert len(skill.versions) == 1

    async def test_it_is_not_asked_of_the_store_again_once_it_is_done(self) -> None:
        store, tenant = CountingStore(), make_tenant_id()
        core = just_announce()

        for _ in range(3):
            await core.ensure(store, tenant, now=START)

        assert store.installs == 1

    async def test_each_tenant_has_its_own_and_sees_no_other(self) -> None:
        store = InMemoryStore()
        one, two = make_tenant_id(), make_tenant_id()
        core = just_announce()

        await core.ensure(store, one, now=START)
        assert await skills_of(store, two) == []
        await core.ensure(store, two, now=START)

        [mine], [yours] = await skills_of(store, one), await skills_of(store, two)
        assert mine.id != yours.id
        assert (mine.tenant_id, yours.tenant_id) == (one, two)

    async def test_two_asks_at_once_install_it_once(self) -> None:
        store, tenant = InMemoryStore(), make_tenant_id()
        core = just_announce()

        first, second = await asyncio.gather(
            core.ensure(store, tenant, now=START), core.ensure(store, tenant, now=START)
        )

        assert sorted([labels(first), labels(second)]) == [[], ["announce@1"]]
        [skill] = await skills_of(store, tenant)
        assert len(skill.versions) == 1


class TestWhatIsLeftAlone:
    async def test_one_that_an_administrator_disabled_stays_disabled_after_a_restart(self) -> None:
        store, tenant = InMemoryStore(), make_tenant_id()
        await just_announce().ensure(store, tenant, now=START)
        [skill] = await skills_of(store, tenant)
        await store.disable(tenant, skill.id)

        restarted = just_announce()  # the same text: not a new version
        enabled = await restarted.ensure(store, tenant, now=START)

        assert enabled == ()
        assert await skills_of(store, tenant) == []
        assert len(await skills_of(store, tenant, enabled_only=False)) == 1

    async def test_one_that_was_installed_and_never_enabled_is_enabled(self) -> None:
        """The service stopped between the two: nobody turned it off, so it is turned on."""
        store, tenant = InMemoryStore(), make_tenant_id()
        announce = Announce(FakeSpeech())
        await store.install(tenant, owner_of(tenant), announce.loaded, now=START)
        assert await skills_of(store, tenant) == []

        enabled = await CoreSkills(announce).ensure(store, tenant, now=START)

        assert labels(enabled) == ["announce@1"]
        [skill] = await skills_of(store, tenant)
        assert skill.enabled is not None

    async def test_a_new_version_of_one_is_enabled_where_it_ships(self) -> None:
        store, tenant = InMemoryStore(), make_tenant_id()
        old = just_announce()
        await old.ensure(store, tenant, now=START)
        newer = CoreSkills(Newer())

        enabled = await newer.ensure(store, tenant, now=START)

        assert labels(enabled) == ["announce@2"]
        [skill] = await skills_of(store, tenant)
        assert skill.enabled is not None
        assert skill.enabled.meta.version == 2
        assert skill.enabled.manifest.effect is Effect.IRREVERSIBLE
        assert newer.recognize(skill) is not None
        assert old.recognize(skill) is None  # the text is not the one that the old one carries

    async def test_a_new_version_is_enabled_even_where_the_old_one_was_disabled(self) -> None:
        store, tenant = InMemoryStore(), make_tenant_id()
        await just_announce().ensure(store, tenant, now=START)
        [skill] = await skills_of(store, tenant)
        await store.disable(tenant, skill.id)

        enabled = await CoreSkills(Newer()).ensure(store, tenant, now=START)

        assert labels(enabled) == ["announce@2"]
        assert len(await skills_of(store, tenant)) == 1


class TestASkillOfThatNameThatSomebodyWrote:
    """The name is the platform's, and the text is what makes a version the platform's."""

    @staticmethod
    async def theirs(store: ActionsStore, tenant: TenantId) -> StoredSkill:
        """A skill of the tenant called ``announce`` that is not the platform's, enabled."""
        text = Announce.source.replace("Say something out loud", "Shout something")
        loaded = parse_skill(text, where="theirs")
        installed = await store.install(tenant, owner_of(tenant), loaded, now=START)
        stored = await store.enable(
            tenant,
            installed.skill_id,
            installed.version,
            granted=loaded.manifest.permissions,
            by=make_user_id(),
            now=START,
        )
        assert stored is not None
        return stored

    async def test_the_platforms_is_added_to_it_as_a_version_and_is_the_one_that_runs(self) -> None:
        store, tenant = InMemoryStore(), make_tenant_id()
        theirs = await self.theirs(store, tenant)
        core = just_announce()

        enabled = await core.ensure(store, tenant, now=START)

        assert labels(enabled) == ["announce@2"]
        [skill] = await skills_of(store, tenant)
        assert skill.id == theirs.id  # one skill of that name, which has two versions now
        assert [meta.version for meta in skill.versions] == [1, 2]
        assert core.recognize(skill) is not None

    async def test_what_they_wrote_is_still_there_and_is_run_as_theirs_when_it_is_enabled(
        self,
    ) -> None:
        store, tenant = InMemoryStore(), make_tenant_id()
        theirs = await self.theirs(store, tenant)
        core = just_announce()
        await core.ensure(store, tenant, now=START)

        back = await store.enable(
            tenant,
            theirs.id,
            1,
            granted=theirs.latest.manifest.permissions,
            by=make_user_id(),
            now=START,
        )

        assert back is not None
        assert back.enabled is not None
        assert back.enabled.meta.version == 1
        assert core.recognize(back) is None  # another text: it is not the platform's

    async def test_and_it_is_not_taken_from_them_again_at_the_next_start(self) -> None:
        store, tenant = InMemoryStore(), make_tenant_id()
        theirs = await self.theirs(store, tenant)
        await just_announce().ensure(store, tenant, now=START)
        await store.enable(
            tenant,
            theirs.id,
            1,
            granted=theirs.latest.manifest.permissions,
            by=make_user_id(),
            now=START,
        )

        enabled = await just_announce().ensure(store, tenant, now=START)

        assert enabled == ()
        [skill] = await skills_of(store, tenant)
        assert skill.enabled is not None
        assert skill.enabled.meta.version == 1


class TestWhatTheyAreGranted:
    async def test_what_a_skill_declares_is_what_it_is_granted_and_nothing_more(self) -> None:
        class WithAHost(CoreSkill):
            source: ClassVar[str] = Announce.source.replace(
                "effect: reversible",
                "effect: reversible\npermissions:\n  network: [api.example.org]",
            )

            async def run(
                self, request: RunRequest, principal: Principal, *, language: str
            ) -> CoreResult:
                return CoreResult()

        store, tenant = InMemoryStore(), make_tenant_id()

        await CoreSkills(WithAHost()).ensure(store, tenant, now=START)

        [skill] = await skills_of(store, tenant)
        assert skill.enabled is not None
        assert skill.enabled.granted is not None
        assert skill.enabled.granted.network == ("api.example.org",)
        assert skill.enabled.granted.secrets == ()

    async def test_the_platforms_announce_declares_nothing_and_is_granted_nothing(self) -> None:
        store, tenant = InMemoryStore(), make_tenant_id()

        await just_announce().ensure(store, tenant, now=START)

        [skill] = await skills_of(store, tenant)
        assert skill.enabled is not None
        assert skill.enabled.granted == SkillPermissions()


class TestWhatIsTheirs:
    @staticmethod
    def real(tenant: TenantId) -> tuple[Announce, StoredSkill]:
        """A stored skill that is exactly the platform's: its name, and the hash of its text."""
        announce = Announce(FakeSpeech())
        skill = stored_skill(announce.loaded.manifest, tenant)
        assert skill.enabled is not None
        meta = dataclasses.replace(skill.enabled.meta, content_sha256=announce.content_sha256)
        enabled = dataclasses.replace(skill.enabled, meta=meta)
        return announce, dataclasses.replace(skill, enabled=enabled)

    def test_a_version_with_the_name_and_the_hash_of_the_platforms_is_the_platforms(self) -> None:
        announce, skill = self.real(make_tenant_id())

        assert CoreSkills(announce).recognize(skill) is announce

    def test_a_skill_of_that_name_that_somebody_wrote_is_not(self) -> None:
        announce = Announce(FakeSpeech())
        theirs = stored_skill(announce.loaded.manifest, make_tenant_id())  # another text

        assert CoreSkills(announce).recognize(theirs) is None

    def test_the_hash_of_the_platforms_text_under_another_name_is_not_either(self) -> None:
        announce, skill = self.real(make_tenant_id())

        assert CoreSkills(announce).recognize(dataclasses.replace(skill, name="shout")) is None

    def test_a_skill_that_is_not_enabled_is_not_run_as_anything(self) -> None:
        announce, skill = self.real(make_tenant_id())

        assert CoreSkills(announce).recognize(dataclasses.replace(skill, enabled=None)) is None

    def test_a_service_without_core_skills_has_none(self) -> None:
        _, skill = self.real(make_tenant_id())

        assert CoreSkills().names == ()
        assert CoreSkills().recognize(skill) is None


class TestWhatThePlatformSays:
    def test_a_skill_is_there_once(self) -> None:
        with pytest.raises(ValueError, match="there once"):
            CoreSkills(Announce(FakeSpeech()), Announce(FakeSpeech()))

    def test_what_a_skill_says_about_itself_is_what_a_message_is_matched_against(self) -> None:
        manifest = just_announce().manifests()["announce"]

        assert manifest.description.startswith("Say something out loud")
        assert any(example.startswith("annuncia") for example in manifest.examples)
        assert any(example.startswith("tell the kitchen") for example in manifest.examples)

    def test_announcing_acts_on_a_device_and_reaches_nothing_on_the_network(self) -> None:
        manifest = just_announce().manifests()["announce"]

        assert manifest.target is SkillTarget.SYSTEM
        assert manifest.effect is Effect.REVERSIBLE
        assert manifest.permissions.network == ()
        assert manifest.permissions.secrets == ()
        assert manifest.required == ("message",)

    def test_a_text_that_is_not_a_skill_is_a_mistake_of_the_platform_at_the_start(self) -> None:
        class Broken(CoreSkill):
            source: ClassVar[str] = "no front matter at all"

            async def run(
                self, request: RunRequest, principal: Principal, *, language: str
            ) -> CoreResult:
                return CoreResult()

        with pytest.raises(SkillInvalidError, match="the core skill Broken"):
            Broken()
