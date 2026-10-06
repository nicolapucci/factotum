"""The catalog of skills: what is installed, what is enabled, and who may say so."""

from pathlib import Path
from uuid import uuid4

import pytest

from factotum.actions.catalog import Catalog
from factotum.actions.core import platform_skills
from factotum.actions.rig import ActionsRig
from factotum.actions.testing import FakeSpeech
from factotum.contracts.actions import SkillEnable, SkillInstall, SkillOwner
from factotum.contracts.errors import (
    ConflictError,
    GatewayUnavailableError,
    PolicyViolationError,
    SkillInvalidError,
    SkillNotFoundError,
)
from factotum.contracts.identity import Owner, Principal, Role, Subject
from factotum.contracts.ids import SkillId, TenantId, UserId
from factotum.contracts.skills import SkillMode

ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = ROOT / "skills" / "examples"

WEATHER = """\
---
name: weather
description: Say what the weather is like somewhere.
examples:
  - what is the weather in Rome
effect: read_only
permissions:
  network: [api.example.org]
  secrets: [WEATHER_KEY]
---
Ask the forecast service.
"""

WEATHER_WIDER = WEATHER.replace("network: [api.example.org]", "network: [api.example.org, b.org]")

NOTES = """\
---
name: notes
description: Keep a note for later.
effect: reversible
---
"""


def catalog_of(
    rig: ActionsRig, *, folder: Path | None = None, enable_folder: bool = False
) -> Catalog:
    return Catalog(rig.store, rig.index, folder=folder, enable_folder=enable_folder, now=rig.clock)


def member(rig: ActionsRig, user: UserId | None = None) -> Principal:
    """A person of the household who is not an administrator, in a hub session."""
    return Principal(
        tenant_id=rig.tenant,
        acts_as=Subject.user(user or UserId(uuid4())),
        roles=frozenset({Role.MEMBER}),
    )


def owned_by(principal: Principal) -> Owner:
    return Owner.of_subject(principal.acts_as)


class TestInstalling:
    async def test_a_skill_is_installed_from_the_text_of_its_skill_file_and_does_not_run(
        self,
    ) -> None:
        rig = ActionsRig()
        catalog = catalog_of(rig)

        detail, created = await catalog.install(rig.principal(None), SkillInstall(source=WEATHER))

        assert created
        assert (detail.name, detail.latest_version, detail.enabled_version) == ("weather", 1, None)
        assert detail.granted is None
        assert detail.owner == rig.shared_owner
        assert [version.enabled for version in detail.versions] == [False]

    async def test_the_very_same_skill_installed_again_is_not_a_new_version(self) -> None:
        rig = ActionsRig()
        catalog = catalog_of(rig)
        first, _ = await catalog.install(rig.principal(None), SkillInstall(source=WEATHER))

        again, created = await catalog.install(rig.principal(None), SkillInstall(source=WEATHER))

        assert not created
        assert (again.id, again.latest_version) == (first.id, 1)

    async def test_a_changed_skill_is_a_new_version_and_the_one_that_runs_stays_the_one_reviewed(
        self,
    ) -> None:
        rig = ActionsRig()
        catalog = catalog_of(rig)
        admin = rig.principal(None)
        first, _ = await catalog.install(admin, SkillInstall(source=WEATHER))
        await catalog.enable(admin, first.id, SkillEnable())

        changed, created = await catalog.install(admin, SkillInstall(source=WEATHER_WIDER))

        assert created
        assert (changed.id, changed.latest_version, changed.enabled_version) == (first.id, 2, 1)
        assert changed.manifest.permissions.network == ("api.example.org", "b.org")
        assert changed.granted is not None
        assert changed.granted.network == ("api.example.org",)

    async def test_a_skill_that_is_not_one_is_refused_and_says_where(self) -> None:
        rig = ActionsRig()

        with pytest.raises(SkillInvalidError, match=r"the skill: .*three dashes"):
            await catalog_of(rig).install(rig.principal(None), SkillInstall(source="just text"))

        assert (await catalog_of(rig).list(rig.principal(None))).skills == ()

    async def test_a_skill_given_as_text_cannot_have_a_script(self) -> None:
        rig = ActionsRig()
        source = WEATHER.replace(
            "effect: read_only", "effect: read_only\nentrypoint: scripts/run.py"
        )

        with pytest.raises(SkillInvalidError, match="scripts come with a folder"):
            await catalog_of(rig).install(rig.principal(None), SkillInstall(source=source))

    async def test_a_persons_own_skill_belongs_to_them(self) -> None:
        rig = ActionsRig()
        mine = member(rig)

        detail, _ = await catalog_of(rig).install(
            mine, SkillInstall(source=NOTES, owner=SkillOwner.USER)
        )

        assert detail.owner == owned_by(mine)


class TestEnabling:
    async def test_enabling_a_version_grants_what_it_declares_and_makes_it_the_one_that_runs(
        self,
    ) -> None:
        rig = ActionsRig()
        catalog = catalog_of(rig)
        admin = rig.principal(None)
        installed, _ = await catalog.install(admin, SkillInstall(source=WEATHER))

        enabled = await catalog.enable(admin, installed.id, SkillEnable())

        assert enabled.enabled_version == 1
        assert enabled.granted == installed.manifest.permissions
        assert enabled.granted is not None
        assert (enabled.granted.network, enabled.granted.secrets) == (
            ("api.example.org",),
            ("WEATHER_KEY",),
        )
        [version] = enabled.versions
        assert (version.enabled, version.enabled_by) == (True, rig.user)
        assert version.enabled_at is not None

    async def test_an_enabled_skill_is_indexed_to_be_found_by_what_it_says_about_itself(
        self,
    ) -> None:
        rig = ActionsRig()
        catalog = catalog_of(rig)
        admin = rig.principal(None)
        installed, _ = await catalog.install(admin, SkillInstall(source=WEATHER))
        assert installed.id not in await rig.store.indexed(
            rig.tenant, rig.shared_owner, rig.embedder.space_id
        )

        await catalog.enable(admin, installed.id, SkillEnable())

        assert installed.id in await rig.store.indexed(
            rig.tenant, rig.shared_owner, rig.embedder.space_id
        )

    async def test_a_skill_is_enabled_even_when_the_embedding_model_cannot_be_reached(
        self,
    ) -> None:
        rig = ActionsRig()
        catalog = catalog_of(rig)
        admin = rig.principal(None)
        installed, _ = await catalog.install(admin, SkillInstall(source=WEATHER))
        rig.embedder.failure = GatewayUnavailableError("the model gateway could not be reached")

        enabled = await catalog.enable(admin, installed.id, SkillEnable())

        assert enabled.enabled_version == 1
        assert installed.id not in await rig.store.indexed(
            rig.tenant, rig.shared_owner, rig.embedder.space_id
        )

    async def test_a_new_version_runs_only_when_it_is_enabled_and_then_its_grants_are_its_own(
        self,
    ) -> None:
        rig = ActionsRig()
        catalog = catalog_of(rig)
        admin = rig.principal(None)
        first, _ = await catalog.install(admin, SkillInstall(source=WEATHER))
        await catalog.enable(admin, first.id, SkillEnable())
        await catalog.install(admin, SkillInstall(source=WEATHER_WIDER))

        newest = await catalog.enable(admin, first.id, SkillEnable())

        assert newest.enabled_version == 2
        assert newest.granted is not None
        assert newest.granted.network == ("api.example.org", "b.org")
        assert [version.enabled for version in newest.versions] == [False, True]

    async def test_an_older_version_can_be_enabled_again_to_go_back(self) -> None:
        rig = ActionsRig()
        catalog = catalog_of(rig)
        admin = rig.principal(None)
        first, _ = await catalog.install(admin, SkillInstall(source=WEATHER))
        await catalog.install(admin, SkillInstall(source=WEATHER_WIDER))
        await catalog.enable(admin, first.id, SkillEnable())

        back = await catalog.enable(admin, first.id, SkillEnable(version=1))

        assert back.enabled_version == 1
        assert back.latest_version == 2
        assert back.granted is not None
        assert back.granted.network == ("api.example.org",)

    async def test_a_version_that_does_not_exist_cannot_be_enabled(self) -> None:
        rig = ActionsRig()
        catalog = catalog_of(rig)
        admin = rig.principal(None)
        installed, _ = await catalog.install(admin, SkillInstall(source=WEATHER))

        with pytest.raises(SkillNotFoundError, match="no such version"):
            await catalog.enable(admin, installed.id, SkillEnable(version=7))

        assert (await catalog.get(admin, installed.id)).enabled_version is None

    async def test_disabling_stops_it_and_keeps_its_versions_to_be_enabled_again(self) -> None:
        rig = ActionsRig()
        catalog = catalog_of(rig)
        admin = rig.principal(None)
        installed, _ = await catalog.install(admin, SkillInstall(source=WEATHER))
        await catalog.enable(admin, installed.id, SkillEnable())

        stopped = await catalog.disable(admin, installed.id)

        assert (stopped.enabled_version, stopped.granted) == (None, None)
        assert len(stopped.versions) == 1
        assert (await catalog.enable(admin, installed.id, SkillEnable())).enabled_version == 1

    async def test_a_deleted_skill_is_gone_with_its_versions(self) -> None:
        rig = ActionsRig()
        catalog = catalog_of(rig)
        admin = rig.principal(None)
        installed, _ = await catalog.install(admin, SkillInstall(source=WEATHER))
        await catalog.enable(admin, installed.id, SkillEnable())

        await catalog.delete(admin, installed.id)

        assert (await catalog.list(admin)).skills == ()
        with pytest.raises(SkillNotFoundError):
            await catalog.get(admin, installed.id)
        assert await rig.store.get_version(rig.tenant, installed.id, 1) is None


class TestListing:
    async def test_the_list_says_what_each_skill_is_and_which_version_runs(self) -> None:
        rig = ActionsRig()
        catalog = catalog_of(rig)
        admin = rig.principal(None)
        weather, _ = await catalog.install(admin, SkillInstall(source=WEATHER))
        await catalog.install(admin, SkillInstall(source=NOTES))
        await catalog.enable(admin, weather.id, SkillEnable())

        listed = (await catalog.list(admin)).skills

        assert [(skill.name, skill.enabled_version) for skill in listed] == [
            ("notes", None),
            ("weather", 1),
        ]
        assert listed[1].description == "Say what the weather is like somewhere."

    async def test_a_person_sees_the_tenants_skills_and_their_own_and_not_another_persons(
        self,
    ) -> None:
        rig = ActionsRig()
        catalog = catalog_of(rig)
        anna, bruno = member(rig), member(rig)
        await catalog.install(rig.principal(None), SkillInstall(source=WEATHER))
        await catalog.install(anna, SkillInstall(source=NOTES, owner=SkillOwner.USER))

        assert [s.name for s in (await catalog.list(anna)).skills] == ["notes", "weather"]
        assert [s.name for s in (await catalog.list(bruno)).skills] == ["weather"]

    async def test_nothing_of_another_tenant_is_seen(self) -> None:
        rig = ActionsRig()
        catalog = catalog_of(rig)
        installed, _ = await catalog.install(rig.principal(None), SkillInstall(source=WEATHER))
        other = TenantId(uuid4())
        stranger = Principal(
            tenant_id=other,
            acts_as=Subject.user(UserId(uuid4())),
            roles=frozenset({Role.ADMIN}),
        )

        assert (await catalog.list(stranger)).skills == ()
        with pytest.raises(SkillNotFoundError):
            await catalog.get(stranger, installed.id)


class TestWhoMayDoWhat:
    async def test_a_member_cannot_manage_the_skills_of_the_tenant(self) -> None:
        rig = ActionsRig()
        catalog = catalog_of(rig)
        installed, _ = await catalog.install(rig.principal(None), SkillInstall(source=WEATHER))
        anna = member(rig)

        with pytest.raises(PolicyViolationError, match="administrators"):
            await catalog.install(anna, SkillInstall(source=NOTES))
        with pytest.raises(PolicyViolationError, match="administrators"):
            await catalog.enable(anna, installed.id, SkillEnable())
        with pytest.raises(PolicyViolationError, match="administrators"):
            await catalog.disable(anna, installed.id)
        with pytest.raises(PolicyViolationError, match="administrators"):
            await catalog.delete(anna, installed.id)

        assert (await catalog.get(anna, installed.id)).enabled_version is None

    async def test_a_member_manages_their_own_skills(self) -> None:
        rig = ActionsRig()
        catalog = catalog_of(rig)
        anna = member(rig)
        mine, _ = await catalog.install(anna, SkillInstall(source=NOTES, owner=SkillOwner.USER))

        enabled = await catalog.enable(anna, mine.id, SkillEnable())
        stopped = await catalog.disable(anna, mine.id)
        await catalog.delete(anna, mine.id)

        assert (enabled.enabled_version, stopped.enabled_version) == (1, None)
        assert (await catalog.list(anna)).skills == ()

    async def test_nobody_manages_another_persons_skill_not_even_an_administrator(self) -> None:
        rig = ActionsRig()
        catalog = catalog_of(rig)
        mine, _ = await catalog.install(
            member(rig, rig.user), SkillInstall(source=NOTES, owner=SkillOwner.USER)
        )
        bruno = member(rig)

        for act in (
            lambda who: catalog.get(who, mine.id),
            lambda who: catalog.enable(who, mine.id, SkillEnable()),
            lambda who: catalog.disable(who, mine.id),
            lambda who: catalog.delete(who, mine.id),
        ):
            with pytest.raises(SkillNotFoundError):
                await act(bruno)

        stranger_admin = Principal(
            tenant_id=rig.tenant,
            acts_as=Subject.user(UserId(uuid4())),
            roles=frozenset({Role.ADMIN}),
        )
        with pytest.raises(SkillNotFoundError):
            await catalog.enable(stranger_admin, mine.id, SkillEnable())
        assert (await catalog.get(member(rig, rig.user), mine.id)).enabled_version is None

    async def test_nothing_is_done_through_a_device(self) -> None:
        rig = ActionsRig()
        catalog = catalog_of(rig)
        installed, _ = await catalog.install(rig.principal(None), SkillInstall(source=WEATHER))
        speaker = rig.principal("Kitchen speaker")
        phone = rig.principal("My phone")
        admin_at_a_device = Principal(
            tenant_id=rig.tenant,
            device_id=phone.device_id,
            acts_as=Subject.user(rig.user),
            roles=frozenset({Role.ADMIN}),
        )

        for who in (speaker, phone, admin_at_a_device):
            with pytest.raises(PolicyViolationError, match="never through a device"):
                await catalog.list(who)
            with pytest.raises(PolicyViolationError, match="never through a device"):
                await catalog.get(who, installed.id)
            with pytest.raises(PolicyViolationError, match="never through a device"):
                await catalog.install(who, SkillInstall(source=NOTES))
            with pytest.raises(PolicyViolationError, match="never through a device"):
                await catalog.enable(who, installed.id, SkillEnable())
            with pytest.raises(PolicyViolationError, match="never through a device"):
                await catalog.disable(who, installed.id)
            with pytest.raises(PolicyViolationError, match="never through a device"):
                await catalog.delete(who, installed.id)
            with pytest.raises(PolicyViolationError, match="never through a device"):
                await catalog.reload(who)

        assert (await catalog.get(rig.principal(None), installed.id)).enabled_version is None

    async def test_a_skill_of_ones_own_is_installed_by_a_person(self) -> None:
        rig = ActionsRig()
        service = Principal(tenant_id=rig.tenant, acts_as=Subject.tenant(rig.tenant))

        with pytest.raises(PolicyViolationError, match="by a person"):
            await catalog_of(rig).install(
                service, SkillInstall(source=NOTES, owner=SkillOwner.USER)
            )

    async def test_an_id_that_is_not_there_is_not_found(self) -> None:
        rig = ActionsRig()

        with pytest.raises(SkillNotFoundError):
            await catalog_of(rig).enable(rig.principal(None), SkillId(uuid4()), SkillEnable())


def write_skill(folder: Path, name: str, text: str, **files: str) -> None:
    (folder / name).mkdir(parents=True, exist_ok=True)
    (folder / name / "SKILL.md").write_text(text, encoding="utf-8", newline="\n")
    for path, content in files.items():
        (folder / name / path).write_text(content, encoding="utf-8", newline="\n")


class TestReadingAFolder:
    async def test_without_a_folder_there_is_nothing_to_read(self) -> None:
        rig = ActionsRig()

        with pytest.raises(ConflictError, match="no folder of skills"):
            await catalog_of(rig).reload(rig.principal(None))

    async def test_what_is_in_the_folder_is_installed_and_not_enabled(self, tmp_path: Path) -> None:
        write_skill(tmp_path, "weather", WEATHER)
        write_skill(tmp_path, "notes", NOTES)
        rig = ActionsRig()
        catalog = catalog_of(rig, folder=tmp_path)

        read = await catalog.reload(rig.principal(None))

        assert sorted(read.installed) == ["notes@1", "weather@1"]
        assert (read.enabled, read.unchanged, read.problems) == ((), 0, ())
        assert [s.enabled_version for s in (await catalog.list(rig.principal(None))).skills] == [
            None,
            None,
        ]

    async def test_reading_it_again_installs_only_what_changed(self, tmp_path: Path) -> None:
        write_skill(tmp_path, "weather", WEATHER)
        write_skill(tmp_path, "notes", NOTES)
        rig = ActionsRig()
        catalog = catalog_of(rig, folder=tmp_path)
        await catalog.reload(rig.principal(None))
        write_skill(tmp_path, "weather", WEATHER_WIDER)

        read = await catalog.reload(rig.principal(None))

        assert (read.installed, read.unchanged) == (("weather@2",), 1)

    async def test_a_skill_that_cannot_be_read_is_reported_and_the_others_are_installed(
        self, tmp_path: Path
    ) -> None:
        write_skill(tmp_path, "weather", WEATHER)
        write_skill(tmp_path, "broken", "no frontmatter here")
        rig = ActionsRig()

        read = await catalog_of(rig, folder=tmp_path).reload(rig.principal(None))

        assert read.installed == ("weather@1",)
        [problem] = read.problems
        assert problem.where == "broken"

    async def test_reading_the_folder_is_for_the_administrators_of_the_tenant(
        self, tmp_path: Path
    ) -> None:
        write_skill(tmp_path, "weather", WEATHER)
        rig = ActionsRig()

        with pytest.raises(PolicyViolationError, match="administrators"):
            await catalog_of(rig, folder=tmp_path).reload(member(rig))

    async def test_what_is_read_is_enabled_as_it_is_read_only_when_told_to(
        self, tmp_path: Path
    ) -> None:
        write_skill(tmp_path, "weather", WEATHER)
        rig = ActionsRig()
        catalog = catalog_of(rig, folder=tmp_path, enable_folder=True)
        admin = rig.principal(None)

        read = await catalog.reload(admin)

        assert read.enabled == ("weather@1",)
        [weather] = (await catalog.list(admin)).skills
        assert weather.enabled_version == 1
        assert weather.id in await rig.store.indexed(
            rig.tenant, rig.shared_owner, rig.embedder.space_id
        )

    async def test_a_folder_that_is_read_again_enables_what_is_new_and_what_was_stopped(
        self, tmp_path: Path
    ) -> None:
        write_skill(tmp_path, "weather", WEATHER)
        rig = ActionsRig()
        catalog = catalog_of(rig, folder=tmp_path, enable_folder=True)
        admin = rig.principal(None)
        await catalog.reload(admin)

        quiet = await catalog.reload(admin)
        write_skill(tmp_path, "weather", WEATHER_WIDER)
        changed = await catalog.reload(admin)
        [weather] = (await catalog.list(admin)).skills
        await catalog.disable(admin, weather.id)
        stopped = await catalog.reload(admin)

        assert (quiet.installed, quiet.enabled, quiet.unchanged) == ((), (), 1)
        assert (changed.installed, changed.enabled) == (("weather@2",), ("weather@2",))
        assert (stopped.installed, stopped.enabled) == ((), ("weather@2",))

    async def test_the_examples_that_come_with_the_project_are_installed_with_their_scripts(
        self,
    ) -> None:
        rig = ActionsRig()
        catalog = catalog_of(rig, folder=EXAMPLES, enable_folder=True)
        admin = rig.principal(None)

        read = await catalog.reload(admin)

        assert read.problems == ()
        assert sorted(read.enabled) == [
            "air-quality@1",
            "garage-door@1",
            "lamp@1",
            "movie-scene@1",
            "restart-router@1",
            "volume@1",
            "weather@1",
        ]
        by_name = {s.name: s.id for s in (await catalog.list(admin)).skills}
        files = await rig.store.get_files(rig.tenant, by_name["lamp"], 1)
        assert files is not None
        assert "scripts/run.py" in files
        # a skill that is instructions has no files: what it says is the text of the document
        assert await rig.store.get_files(rig.tenant, by_name["movie-scene"], 1) == {}


class TestTheSkillsOfThePlatform:
    """ADR 0026: what the platform provides is there, enabled, for a tenant that has not yet said a
    word, so that an administrator who opens the catalog first sees what the assistant can do."""

    @staticmethod
    def with_core(rig: ActionsRig) -> Catalog:
        return Catalog(rig.store, rig.index, core=platform_skills(FakeSpeech()), now=rig.clock)

    async def test_what_an_administrator_first_sees_is_the_skills_of_the_platform_enabled(
        self,
    ) -> None:
        rig = ActionsRig()

        [skill] = (await self.with_core(rig).list(rig.principal(None))).skills

        assert (skill.name, skill.owner, skill.mode) == (
            "announce",
            rig.shared_owner,
            SkillMode.INSTRUCTIONS,
        )
        assert (skill.latest_version, skill.enabled_version) == (1, 1)

    async def test_and_they_are_found_by_meaning_from_then_on(self) -> None:
        rig = ActionsRig()

        [skill] = (await self.with_core(rig).list(rig.principal(None))).skills

        indexed = await rig.store.indexed(rig.tenant, rig.shared_owner, rig.embedder.space_id)
        assert indexed == {skill.id}

    async def test_so_does_a_member_of_the_household(self) -> None:
        rig = ActionsRig()

        listed = await self.with_core(rig).list(member(rig))

        assert [skill.name for skill in listed.skills] == ["announce"]

    async def test_what_is_granted_to_it_is_what_it_declares_and_it_was_nobodys_review(
        self,
    ) -> None:
        rig = ActionsRig()
        catalog = self.with_core(rig)
        admin = rig.principal(None)
        [skill] = (await catalog.list(admin)).skills

        detail = await catalog.get(admin, skill.id)

        assert detail.granted == detail.manifest.permissions
        assert detail.versions[0].enabled
        assert detail.versions[0].enabled_by is None  # the platform enabled it, and no person

    async def test_a_device_is_not_a_way_to_have_them_installed(self) -> None:
        rig = ActionsRig()

        with pytest.raises(PolicyViolationError, match="never through a device"):
            await self.with_core(rig).list(rig.principal("Kitchen speaker"))

        assert await rig.store.list_skills(rig.tenant, [rig.shared_owner]) == []

    async def test_a_catalog_that_was_given_none_has_none(self) -> None:
        rig = ActionsRig()

        assert (await catalog_of(rig).list(rig.principal(None))).skills == ()

    async def test_an_administrator_who_installs_a_skill_first_finds_the_platforms_there_too(
        self,
    ) -> None:
        rig = ActionsRig()
        catalog = self.with_core(rig)
        admin = rig.principal(None)

        await catalog.install(admin, SkillInstall(source=NOTES))

        # what the store has, and not what a listing would have put there
        stored = await rig.store.list_skills(rig.tenant, [rig.shared_owner])
        assert [skill.name for skill in stored] == ["announce", "notes"]
        announce = stored[0]
        assert [meta.version for meta in announce.versions] == [1]
        assert announce.enabled is not None

    async def test_a_folder_that_is_read_finds_them_there_too(self, tmp_path: Path) -> None:
        write_skill(tmp_path, "weather", WEATHER)
        rig = ActionsRig()
        catalog = Catalog(
            rig.store, rig.index, folder=tmp_path, core=platform_skills(FakeSpeech()), now=rig.clock
        )

        await catalog.reload(rig.principal(None))

        stored = await rig.store.list_skills(rig.tenant, [rig.shared_owner], enabled_only=True)
        assert [skill.name for skill in stored] == ["announce"]  # weather is installed, not enabled

    async def test_one_that_an_administrator_disables_stays_disabled(self) -> None:
        rig = ActionsRig()
        catalog = self.with_core(rig)
        admin = rig.principal(None)
        [skill] = (await catalog.list(admin)).skills
        await catalog.disable(admin, skill.id)

        [still] = (await self.with_core(rig).list(admin)).skills  # a service that was restarted

        assert still.enabled_version is None

    async def test_two_tenants_are_each_given_their_own_and_see_no_other(self) -> None:
        one, two = ActionsRig(), ActionsRig()
        speech = FakeSpeech()
        core = platform_skills(speech)
        mine = Catalog(one.store, one.index, core=core, now=one.clock)
        yours = Catalog(two.store, two.index, core=core, now=two.clock)

        [first] = (await mine.list(one.principal(None))).skills
        [second] = (await yours.list(two.principal(None))).skills

        assert first.id != second.id
        assert (first.tenant_id, second.tenant_id) == (one.tenant, two.tenant)
