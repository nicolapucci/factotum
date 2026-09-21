"""What every store promises, run against the one in memory and the one in Postgres."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from factotum.actions.skills import LoadedSkill
from factotum.actions.store import ActionsStore, PendingRow, VectorRow
from factotum.contracts.audit import ActionResult, AuditRecord
from factotum.contracts.authorization import DecisionOutcome
from factotum.contracts.effects import Effect
from factotum.contracts.identity import Owner, OwnerKind, Subject
from factotum.contracts.ids import AuditId, DeviceId, SkillId, TenantId, UserId
from factotum.contracts.skills import SkillManifest, SkillPermissions

pytestmark = pytest.mark.asyncio(loop_scope="module")

NOW = datetime(2026, 9, 21, 9, 30, tzinfo=UTC)
SPACE = "space-a"


@pytest.fixture
def tenant() -> TenantId:
    return TenantId(uuid4())


@pytest.fixture
def owner(tenant: TenantId) -> Owner:
    return Owner.of_subject(Subject.tenant(tenant))


def a_person() -> Owner:
    return Owner(kind=OwnerKind.USER, id=uuid4())


def a_skill(
    name: str = "lamp", marker: str = "one", files: dict[str, bytes] | None = None
) -> LoadedSkill:
    """A skill; ``marker`` is what makes two versions of it differ."""
    return LoadedSkill(
        manifest=SkillManifest(name=name, description=f"Skill {name}.", effect=Effect.REVERSIBLE),
        instructions=f"Instructions {marker}.",
        source=f"---\nname: {name}\n---\n{marker}\n",
        files=files or {},
    )


async def installed(
    store: ActionsStore, tenant: TenantId, owner: Owner, name: str = "lamp", marker: str = "one"
) -> SkillId:
    result = await store.install(tenant, owner, a_skill(name, marker), now=NOW)
    return result.skill_id


class TestInstalling:
    async def test_a_skill_is_installed_as_its_first_version(
        self, store: ActionsStore, tenant: TenantId, owner: Owner
    ) -> None:
        result = await store.install(tenant, owner, a_skill(), now=NOW)

        assert (result.version, result.created) == (1, True)
        stored = await store.get_skill(tenant, result.skill_id)
        assert stored is not None
        assert (stored.name, stored.owner, stored.created_at) == ("lamp", owner, NOW)
        assert stored.latest.manifest.name == "lamp"
        assert stored.latest.instructions == "Instructions one."
        assert [m.version for m in stored.versions] == [1]
        assert stored.enabled is None

    async def test_the_very_same_skill_is_not_a_new_version(
        self, store: ActionsStore, tenant: TenantId, owner: Owner
    ) -> None:
        first = await store.install(tenant, owner, a_skill(), now=NOW)

        again = await store.install(tenant, owner, a_skill(), now=NOW + timedelta(hours=1))

        assert (again.skill_id, again.version, again.created) == (first.skill_id, 1, False)
        stored = await store.get_skill(tenant, first.skill_id)
        assert stored is not None
        assert len(stored.versions) == 1

    async def test_a_skill_that_changed_is_the_next_version_and_the_latest_is_the_new_one(
        self, store: ActionsStore, tenant: TenantId, owner: Owner
    ) -> None:
        first = await store.install(tenant, owner, a_skill(marker="one"), now=NOW)

        second = await store.install(tenant, owner, a_skill(marker="two"), now=NOW)

        assert (second.skill_id, second.version, second.created) == (first.skill_id, 2, True)
        stored = await store.get_skill(tenant, first.skill_id)
        assert stored is not None
        assert stored.latest.instructions == "Instructions two."
        assert [m.version for m in stored.versions] == [1, 2]

    async def test_going_back_to_what_a_skill_was_is_not_a_new_version(
        self, store: ActionsStore, tenant: TenantId, owner: Owner
    ) -> None:
        await store.install(tenant, owner, a_skill(marker="one"), now=NOW)
        await store.install(tenant, owner, a_skill(marker="two"), now=NOW)

        back = await store.install(tenant, owner, a_skill(marker="one"), now=NOW)

        assert (back.version, back.created) == (1, False)

    async def test_the_files_beside_the_skill_are_part_of_the_version(
        self, store: ActionsStore, tenant: TenantId, owner: Owner
    ) -> None:
        one = await store.install(
            tenant,
            owner,
            a_skill(files={"scripts/run.py": b"print(1)", "data": b"\x00\xff"}),
            now=NOW,
        )
        two = await store.install(
            tenant, owner, a_skill(files={"scripts/run.py": b"print(2)"}), now=NOW
        )

        assert two.version == 2
        assert await store.get_files(tenant, one.skill_id, 1) == {
            "scripts/run.py": b"print(1)",
            "data": b"\x00\xff",
        }
        assert await store.get_files(tenant, one.skill_id, 2) == {"scripts/run.py": b"print(2)"}
        assert await store.get_files(tenant, one.skill_id, 3) is None

    async def test_two_owners_can_each_have_a_skill_of_the_same_name(
        self, store: ActionsStore, tenant: TenantId, owner: Owner
    ) -> None:
        mine = a_person()

        shared = await store.install(tenant, owner, a_skill(), now=NOW)
        personal = await store.install(tenant, mine, a_skill(), now=NOW)

        assert shared.skill_id != personal.skill_id
        assert [s.id for s in await store.list_skills(tenant, [mine])] == [personal.skill_id]

    async def test_two_installs_at_once_are_one_version(
        self, store: ActionsStore, tenant: TenantId, owner: Owner
    ) -> None:
        import asyncio

        results = await asyncio.gather(
            *[store.install(tenant, owner, a_skill(marker="new"), now=NOW) for _ in range(5)]
        )

        assert {r.version for r in results} == {1}
        assert sum(r.created for r in results) == 1


class TestOneVersion:
    async def test_any_version_can_be_read_whichever_it_is(
        self, store: ActionsStore, tenant: TenantId, owner: Owner
    ) -> None:
        skill_id = await installed(store, tenant, owner, marker="one")
        await store.install(tenant, owner, a_skill(marker="two"), now=NOW)
        await store.enable(
            tenant,
            skill_id,
            1,
            granted=SkillPermissions(network=("a.example.org",)),
            by=None,
            now=NOW,
        )

        first = await store.get_version(tenant, skill_id, 1)
        second = await store.get_version(tenant, skill_id, 2)

        assert first is not None
        assert second is not None
        assert (first.instructions, second.instructions) == (
            "Instructions one.",
            "Instructions two.",
        )
        assert first.granted == SkillPermissions(network=("a.example.org",))
        assert second.granted is None
        assert first.manifest.name == "lamp"

    async def test_a_version_that_is_not_there_or_is_not_the_tenants_is_nothing(
        self, store: ActionsStore, tenant: TenantId, owner: Owner
    ) -> None:
        skill_id = await installed(store, tenant, owner)

        assert await store.get_version(tenant, skill_id, 2) is None
        assert await store.get_version(tenant, SkillId(uuid4()), 1) is None
        assert await store.get_version(TenantId(uuid4()), skill_id, 1) is None


class TestListing:
    async def test_skills_are_listed_by_name_for_the_owners_asked(
        self, store: ActionsStore, tenant: TenantId, owner: Owner
    ) -> None:
        mine = a_person()
        for name in ("weather", "lamp", "volume"):
            await installed(store, tenant, owner, name)
        await installed(store, tenant, mine, "diary")
        await installed(store, tenant, a_person(), "somebody-elses")

        both = await store.list_skills(tenant, [owner, mine])
        only_shared = await store.list_skills(tenant, [owner])

        assert [s.name for s in both] == ["diary", "lamp", "volume", "weather"]
        assert [s.name for s in only_shared] == ["lamp", "volume", "weather"]
        assert await store.list_skills(tenant, []) == []

    async def test_the_skills_that_run_are_the_enabled_ones(
        self, store: ActionsStore, tenant: TenantId, owner: Owner
    ) -> None:
        lamp = await installed(store, tenant, owner, "lamp")
        await installed(store, tenant, owner, "weather")
        await store.enable(tenant, lamp, 1, granted=SkillPermissions(), by=None, now=NOW)

        found = await store.list_skills(tenant, [owner], enabled_only=True)

        assert [s.name for s in found] == ["lamp"]

    async def test_no_tenant_sees_the_skills_of_another(
        self, store: ActionsStore, tenant: TenantId, owner: Owner
    ) -> None:
        skill_id = await installed(store, tenant, owner)
        stranger = TenantId(uuid4())

        assert await store.list_skills(stranger, [owner]) == []
        assert await store.get_skill(stranger, skill_id) is None
        assert await store.get_files(stranger, skill_id, 1) is None


class TestEnabling:
    async def test_a_version_runs_once_it_is_enabled_with_what_it_was_granted(
        self, store: ActionsStore, tenant: TenantId, owner: Owner
    ) -> None:
        skill_id = await installed(store, tenant, owner)
        granted = SkillPermissions(network=("api.example.org",), secrets=("KEY",))
        reviewer = UserId(uuid4())

        stored = await store.enable(
            tenant, skill_id, 1, granted=granted, by=reviewer, now=NOW + timedelta(minutes=5)
        )

        assert stored is not None
        assert stored.enabled is not None
        assert stored.enabled.granted == granted
        assert stored.enabled.meta.enabled_by == reviewer
        assert stored.enabled.meta.enabled_at == NOW + timedelta(minutes=5)

    async def test_a_new_version_does_not_run_until_it_is_enabled_too(
        self, store: ActionsStore, tenant: TenantId, owner: Owner
    ) -> None:
        skill_id = await installed(store, tenant, owner, marker="one")
        await store.enable(tenant, skill_id, 1, granted=SkillPermissions(), by=None, now=NOW)

        await store.install(tenant, owner, a_skill(marker="two"), now=NOW)
        stored = await store.get_skill(tenant, skill_id)

        assert stored is not None
        assert stored.latest.instructions == "Instructions two."
        assert stored.enabled is not None
        assert stored.enabled.meta.version == 1
        assert stored.enabled.instructions == "Instructions one."

    async def test_another_version_can_be_enabled_and_the_old_one_stops_running(
        self, store: ActionsStore, tenant: TenantId, owner: Owner
    ) -> None:
        skill_id = await installed(store, tenant, owner, marker="one")
        await store.install(tenant, owner, a_skill(marker="two"), now=NOW)
        await store.enable(tenant, skill_id, 1, granted=SkillPermissions(), by=None, now=NOW)

        stored = await store.enable(
            tenant,
            skill_id,
            2,
            granted=SkillPermissions(network=("a.example.org",)),
            by=None,
            now=NOW,
        )

        assert stored is not None
        assert stored.enabled is not None
        assert stored.enabled.meta.version == 2
        assert stored.enabled.granted == SkillPermissions(network=("a.example.org",))

    async def test_a_skill_can_be_stopped_and_its_versions_stay(
        self, store: ActionsStore, tenant: TenantId, owner: Owner
    ) -> None:
        skill_id = await installed(store, tenant, owner)
        await store.enable(tenant, skill_id, 1, granted=SkillPermissions(), by=None, now=NOW)

        stored = await store.disable(tenant, skill_id)

        assert stored is not None
        assert stored.enabled is None
        assert [m.version for m in stored.versions] == [1]

    async def test_what_does_not_exist_cannot_be_enabled_or_stopped(
        self, store: ActionsStore, tenant: TenantId, owner: Owner
    ) -> None:
        skill_id = await installed(store, tenant, owner)

        assert (
            await store.enable(tenant, skill_id, 9, granted=SkillPermissions(), by=None, now=NOW)
            is None
        )
        assert (
            await store.enable(
                tenant, SkillId(uuid4()), 1, granted=SkillPermissions(), by=None, now=NOW
            )
            is None
        )
        assert await store.disable(tenant, SkillId(uuid4())) is None

    async def test_another_tenant_cannot_enable_or_stop_a_skill(
        self, store: ActionsStore, tenant: TenantId, owner: Owner
    ) -> None:
        skill_id = await installed(store, tenant, owner)
        stranger = TenantId(uuid4())

        assert (
            await store.enable(stranger, skill_id, 1, granted=SkillPermissions(), by=None, now=NOW)
            is None
        )
        assert await store.disable(stranger, skill_id) is None
        stored = await store.get_skill(tenant, skill_id)
        assert stored is not None
        assert stored.enabled is None


class TestDeleting:
    async def test_a_skill_goes_with_its_versions_files_and_vectors(
        self, store: ActionsStore, tenant: TenantId, owner: Owner
    ) -> None:
        one = await store.install(tenant, owner, a_skill(files={"scripts/run.py": b"x"}), now=NOW)
        await store.enable(tenant, one.skill_id, 1, granted=SkillPermissions(), by=None, now=NOW)
        await store.put_vectors(tenant, one.skill_id, 1, SPACE, [VectorRow("a", (1.0, 0.0))])

        assert await store.delete(tenant, one.skill_id) is True

        assert await store.get_skill(tenant, one.skill_id) is None
        assert await store.get_files(tenant, one.skill_id, 1) is None
        assert await store.indexed(tenant, owner, SPACE) == set()
        assert await store.delete(tenant, one.skill_id) is False

    async def test_another_tenant_cannot_delete_it(
        self, store: ActionsStore, tenant: TenantId, owner: Owner
    ) -> None:
        skill_id = await installed(store, tenant, owner)

        assert await store.delete(TenantId(uuid4()), skill_id) is False
        assert await store.get_skill(tenant, skill_id) is not None

    async def test_what_was_audited_about_it_stays(
        self, store: ActionsStore, tenant: TenantId, owner: Owner
    ) -> None:
        skill_id = await installed(store, tenant, owner)
        await store.add_audit(record(tenant, skill_id))

        await store.delete(tenant, skill_id)

        assert len(await store.list_audit(tenant, before=None, limit=10)) == 1


class TestByMeaning:
    async def enabled(
        self, store: ActionsStore, tenant: TenantId, owner: Owner, name: str, rows: list[VectorRow]
    ) -> SkillId:
        skill_id = await installed(store, tenant, owner, name)
        await store.enable(tenant, skill_id, 1, granted=SkillPermissions(), by=None, now=NOW)
        await store.put_vectors(tenant, skill_id, 1, SPACE, rows)
        return skill_id

    async def test_the_skills_nearest_to_a_message_come_first_with_what_matched(
        self, store: ActionsStore, tenant: TenantId, owner: Owner
    ) -> None:
        lamp = await self.enabled(
            store,
            tenant,
            owner,
            "lamp",
            [VectorRow("turn on the lamp", (1.0, 0.0)), VectorRow("switch off", (0.8, 0.6))],
        )
        weather = await self.enabled(
            store, tenant, owner, "weather", [VectorRow("is it raining", (0.0, 1.0))]
        )

        found = await store.search_vectors(tenant, owner, SPACE, (1.0, 0.1), limit=5)

        assert [m.skill_id for m in found] == [lamp, weather]
        assert (found[0].matched, found[0].version) == ("turn on the lamp", 1)
        assert found[0].score > found[1].score
        assert found[0].score == pytest.approx(0.995, abs=0.01)

    async def test_a_skill_is_as_near_as_the_nearest_thing_it_says_and_is_found_once(
        self, store: ActionsStore, tenant: TenantId, owner: Owner
    ) -> None:
        await self.enabled(
            store,
            tenant,
            owner,
            "lamp",
            [
                VectorRow("far", (0.0, 1.0)),
                VectorRow("near", (1.0, 0.0)),
                VectorRow("mid", (0.6, 0.8)),
            ],
        )

        [found] = await store.search_vectors(tenant, owner, SPACE, (1.0, 0.0), limit=5)

        assert found.matched == "near"
        assert found.score == pytest.approx(1.0)

    async def test_only_as_many_as_asked_for_are_found(
        self, store: ActionsStore, tenant: TenantId, owner: Owner
    ) -> None:
        for number in range(4):
            await self.enabled(
                store, tenant, owner, f"skill-{number}", [VectorRow("x", (1.0, float(number)))]
            )

        assert len(await store.search_vectors(tenant, owner, SPACE, (1.0, 0.0), limit=2)) == 2

    async def test_a_skill_that_does_not_run_is_not_found(
        self, store: ActionsStore, tenant: TenantId, owner: Owner
    ) -> None:
        skill_id = await installed(store, tenant, owner)
        await store.put_vectors(tenant, skill_id, 1, SPACE, [VectorRow("x", (1.0, 0.0))])

        assert await store.search_vectors(tenant, owner, SPACE, (1.0, 0.0), limit=5) == []
        assert await store.indexed(tenant, owner, SPACE) == set()

    async def test_the_vectors_of_a_version_that_does_not_run_are_not_the_skills(
        self, store: ActionsStore, tenant: TenantId, owner: Owner
    ) -> None:
        skill_id = await self.enabled(store, tenant, owner, "lamp", [VectorRow("x", (1.0, 0.0))])
        await store.install(tenant, owner, a_skill(marker="two"), now=NOW)
        await store.put_vectors(tenant, skill_id, 2, SPACE, [VectorRow("y", (0.0, 1.0))])

        [found] = await store.search_vectors(tenant, owner, SPACE, (0.0, 1.0), limit=5)

        assert (found.version, found.matched) == (1, "x")

    async def test_the_vectors_of_a_version_are_replaced_when_they_are_made_again(
        self, store: ActionsStore, tenant: TenantId, owner: Owner
    ) -> None:
        skill_id = await self.enabled(store, tenant, owner, "lamp", [VectorRow("old", (1.0, 0.0))])

        await store.put_vectors(tenant, skill_id, 1, SPACE, [VectorRow("new", (0.0, 1.0))])

        [found] = await store.search_vectors(tenant, owner, SPACE, (0.0, 1.0), limit=5)
        assert found.matched == "new"

    async def test_vectors_of_another_space_are_not_compared(
        self, store: ActionsStore, tenant: TenantId, owner: Owner
    ) -> None:
        skill_id = await self.enabled(store, tenant, owner, "lamp", [VectorRow("x", (1.0, 0.0))])

        assert await store.search_vectors(tenant, owner, "another-space", (1.0, 0.0), limit=5) == []
        assert await store.indexed(tenant, owner, "another-space") == set()
        assert await store.indexed(tenant, owner, SPACE) == {skill_id}

    async def test_the_skills_of_one_owner_are_not_found_by_another(
        self, store: ActionsStore, tenant: TenantId, owner: Owner
    ) -> None:
        mine = a_person()
        await self.enabled(store, tenant, mine, "diary", [VectorRow("x", (1.0, 0.0))])

        assert await store.search_vectors(tenant, owner, SPACE, (1.0, 0.0), limit=5) == []
        assert len(await store.search_vectors(tenant, mine, SPACE, (1.0, 0.0), limit=5)) == 1

    async def test_no_tenant_finds_the_skills_of_another(
        self, store: ActionsStore, tenant: TenantId, owner: Owner
    ) -> None:
        await self.enabled(store, tenant, owner, "lamp", [VectorRow("x", (1.0, 0.0))])
        stranger = TenantId(uuid4())

        assert await store.search_vectors(stranger, owner, SPACE, (1.0, 0.0), limit=5) == []
        assert await store.indexed(stranger, owner, SPACE) == set()

    async def test_the_vectors_of_a_model_that_is_not_the_owners_any_more_are_forgotten(
        self, store: ActionsStore, tenant: TenantId, owner: Owner
    ) -> None:
        old = await self.enabled(store, tenant, owner, "old", [VectorRow("x", (1.0, 0.0))])
        current = await installed(store, tenant, owner, "current")
        await store.enable(tenant, current, 1, granted=SkillPermissions(), by=None, now=NOW)
        await store.put_vectors(
            tenant, current, 1, "the-new-space", [VectorRow("y", (1.0, 0.0, 0.0))]
        )

        dropped = await store.forget_other_spaces(tenant, owner, "the-new-space")

        assert dropped == 1
        assert await store.indexed(tenant, owner, SPACE) == set()
        assert await store.indexed(tenant, owner, "the-new-space") == {current}
        assert old not in await store.indexed(tenant, owner, "the-new-space")

    async def test_forgetting_touches_only_the_vectors_of_that_owner_and_tenant(
        self, store: ActionsStore, tenant: TenantId, owner: Owner
    ) -> None:
        mine = a_person()
        stranger = TenantId(uuid4())
        await self.enabled(store, tenant, mine, "diary", [VectorRow("x", (1.0, 0.0))])
        await self.enabled(
            store,
            stranger,
            Owner.of_subject(Subject.tenant(stranger)),
            "lamp",
            [VectorRow("x", (1.0, 0.0))],
        )

        assert await store.forget_other_spaces(tenant, owner, "new") == 0
        assert len(await store.search_vectors(tenant, mine, SPACE, (1.0, 0.0), limit=5)) == 1


def pending(
    tenant: TenantId,
    subject: Subject,
    device: DeviceId | None = None,
    *,
    proposal: dict[str, object] | None = None,
    expires: timedelta = timedelta(seconds=60),
) -> PendingRow:
    return PendingRow(
        id=uuid4(),
        tenant_id=tenant,
        subject=subject,
        device_id=device,
        proposal=proposal or {"skill": "lamp", "params": {"state": "on"}},
        created_at=NOW,
        expires_at=NOW + expires,
    )


class TestConfirmations:
    async def test_what_is_waited_for_is_found_for_that_person_on_that_device(
        self, store: ActionsStore, tenant: TenantId
    ) -> None:
        subject = Subject.tenant(tenant)
        device = DeviceId(uuid4())
        waiting = pending(tenant, subject, device, proposal={"skill": "garage-door", "n": [1, 2]})
        await store.put_pending(waiting)

        found = await store.get_pending(tenant, subject, device)

        assert found == waiting
        assert found is not None
        assert found.proposal == {"skill": "garage-door", "n": [1, 2]}

    async def test_it_is_not_found_for_another_device_another_person_or_another_tenant(
        self, store: ActionsStore, tenant: TenantId
    ) -> None:
        subject = Subject.tenant(tenant)
        device = DeviceId(uuid4())
        await store.put_pending(pending(tenant, subject, device))

        assert await store.get_pending(tenant, subject, DeviceId(uuid4())) is None
        assert await store.get_pending(tenant, subject, None) is None
        assert await store.get_pending(tenant, Subject.user(UserId(uuid4())), device) is None
        assert await store.get_pending(TenantId(uuid4()), subject, device) is None

    async def test_a_person_with_no_device_is_a_key_of_its_own(
        self, store: ActionsStore, tenant: TenantId
    ) -> None:
        subject = Subject.user(UserId(uuid4()))
        await store.put_pending(pending(tenant, subject, None))

        assert await store.get_pending(tenant, subject, None) is not None
        assert await store.get_pending(tenant, subject, DeviceId(uuid4())) is None

    async def test_there_is_one_at_a_time_and_a_new_one_replaces_the_old(
        self, store: ActionsStore, tenant: TenantId
    ) -> None:
        subject = Subject.tenant(tenant)
        device = DeviceId(uuid4())
        old = pending(tenant, subject, device, proposal={"which": "old"})
        new = pending(tenant, subject, device, proposal={"which": "new"})
        await store.put_pending(old)

        await store.put_pending(new)

        found = await store.get_pending(tenant, subject, device)
        assert found is not None
        assert found.proposal == {"which": "new"}
        assert await store.take_pending(tenant, old.id) is None

    async def test_what_was_waited_for_is_taken_once(
        self, store: ActionsStore, tenant: TenantId
    ) -> None:
        subject = Subject.tenant(tenant)
        waiting = pending(tenant, subject)
        await store.put_pending(waiting)

        first = await store.take_pending(tenant, waiting.id)
        second = await store.take_pending(tenant, waiting.id)

        assert first == waiting
        assert second is None
        assert await store.get_pending(tenant, subject, None) is None

    async def test_an_answer_that_arrives_twice_at_once_does_the_thing_once(
        self, store: ActionsStore, tenant: TenantId
    ) -> None:
        import asyncio

        waiting = pending(tenant, Subject.tenant(tenant))
        await store.put_pending(waiting)

        taken = await asyncio.gather(*[store.take_pending(tenant, waiting.id) for _ in range(8)])

        assert sum(row is not None for row in taken) == 1

    async def test_another_tenant_cannot_take_it(
        self, store: ActionsStore, tenant: TenantId
    ) -> None:
        waiting = pending(tenant, Subject.tenant(tenant))
        await store.put_pending(waiting)

        assert await store.take_pending(TenantId(uuid4()), waiting.id) is None
        assert await store.take_pending(tenant, uuid4()) is None
        assert await store.take_pending(tenant, waiting.id) == waiting

    async def test_a_confirmation_that_expired_is_still_found_for_the_service_to_judge(
        self, store: ActionsStore, tenant: TenantId
    ) -> None:
        subject = Subject.tenant(tenant)
        expired = pending(tenant, subject, expires=timedelta(seconds=1))
        await store.put_pending(expired)

        found = await store.get_pending(tenant, subject, None)

        assert found is not None
        assert found.expires_at == NOW + timedelta(seconds=1)


def record(
    tenant: TenantId,
    skill_id: SkillId | None = None,
    *,
    at: datetime = NOW,
    name: str = "lamp",
    device: DeviceId | None = None,
) -> AuditRecord:
    return AuditRecord(
        id=AuditId(uuid4()),
        tenant_id=tenant,
        occurred_at=at,
        device_id=device,
        acts_as=Subject.tenant(tenant),
        skill_id=skill_id or SkillId(uuid4()),
        skill_name=name,
        params={"state": "on", "brightness": 40, "smooth": True, "label": "x"},
        decision=DecisionOutcome.ALLOW,
        result=ActionResult.EXECUTED,
        detail="done",
    )


class TestAudit:
    async def test_a_record_is_kept_as_it_was_written(
        self, store: ActionsStore, tenant: TenantId
    ) -> None:
        written = record(tenant, device=DeviceId(uuid4()))
        await store.add_audit(written)

        assert await store.list_audit(tenant, before=None, limit=10) == [written]

    async def test_the_latest_come_first_and_only_as_many_as_asked_for(
        self, store: ActionsStore, tenant: TenantId
    ) -> None:
        records = [record(tenant, at=NOW + timedelta(minutes=n)) for n in range(5)]
        for one in records:
            await store.add_audit(one)

        latest = await store.list_audit(tenant, before=None, limit=3)
        older = await store.list_audit(tenant, before=latest[-1].occurred_at, limit=10)

        assert [r.id for r in latest] == [r.id for r in reversed(records[2:])]
        assert [r.id for r in older] == [records[1].id, records[0].id]

    async def test_they_can_be_asked_for_by_skill_and_by_device(
        self, store: ActionsStore, tenant: TenantId
    ) -> None:
        kitchen = DeviceId(uuid4())
        await store.add_audit(record(tenant, name="lamp", device=kitchen))
        await store.add_audit(record(tenant, name="weather", device=kitchen))
        await store.add_audit(record(tenant, name="lamp", device=DeviceId(uuid4())))

        assert len(await store.list_audit(tenant, before=None, limit=10, skill_name="lamp")) == 2
        assert len(await store.list_audit(tenant, before=None, limit=10, device_id=kitchen)) == 2
        assert (
            len(
                await store.list_audit(
                    tenant, before=None, limit=10, skill_name="lamp", device_id=kitchen
                )
            )
            == 1
        )

    async def test_no_tenant_reads_the_trail_of_another(
        self, store: ActionsStore, tenant: TenantId
    ) -> None:
        await store.add_audit(record(tenant))

        assert await store.list_audit(TenantId(uuid4()), before=None, limit=10) == []

    async def test_a_record_is_not_a_uuid_only_it_is_a_whole_action(
        self, store: ActionsStore, tenant: TenantId
    ) -> None:
        written = record(tenant)
        await store.add_audit(written)

        [read] = await store.list_audit(tenant, before=None, limit=1)

        assert isinstance(read.id, UUID)
        assert read.params == written.params
        assert (read.decision, read.result, read.detail) == (
            DecisionOutcome.ALLOW,
            ActionResult.EXECUTED,
            "done",
        )
