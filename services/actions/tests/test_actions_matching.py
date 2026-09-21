import asyncio
from typing import Any
from uuid import uuid4

import pytest

from factotum.actions.matching import (
    Candidates,
    SkillIndex,
    SkillMatcher,
    owners_of,
    what_a_skill_says_about_itself,
)
from factotum.actions.skills import LoadedSkill
from factotum.actions.store import ActionsStore, InMemoryStore, StoredSkill
from factotum.actions.testing import LAMP, ROUTER, START, VOLUME, WEATHER, FakeEmbedder
from factotum.contracts.errors import EmbeddingNotConfiguredError, GatewayUnavailableError
from factotum.contracts.identity import Owner, OwnerKind, Principal, Role, Subject
from factotum.contracts.ids import TenantId, UserId
from factotum.contracts.skills import SkillManifest, SkillPermissions
from factotum.contracts.testing import make_tenant_id, make_user_id

TENANT = make_tenant_id()
USER = make_user_id()


def shared(tenant: TenantId = TENANT) -> Principal:
    return Principal(tenant_id=tenant, acts_as=Subject.tenant(tenant))


def person(tenant: TenantId = TENANT, user: UserId = USER) -> Principal:
    return Principal(tenant_id=tenant, acts_as=Subject.user(user), roles=frozenset({Role.MEMBER}))


async def enable(
    store: ActionsStore,
    manifest: SkillManifest,
    tenant: TenantId = TENANT,
    owner: Owner | None = None,
) -> StoredSkill:
    """A skill that is installed and runs."""
    owner = owner or Owner.of_subject(Subject.tenant(tenant))
    installed = await store.install(
        tenant,
        owner,
        LoadedSkill(manifest, "", f"---\nname: {manifest.name}\n---\n", {"scripts/run.py": b"x"}),
        now=START,
    )
    stored = await store.enable(
        tenant,
        installed.skill_id,
        installed.version,
        granted=SkillPermissions(),
        by=None,
        now=START,
    )
    assert stored is not None
    return stored


class Rig:
    def __init__(self, **settings: Any) -> None:
        self.store = InMemoryStore()
        self.embedder = FakeEmbedder()
        self.index = SkillIndex(self.store, self.embedder)
        self.matcher = SkillMatcher(self.store, self.embedder, self.index, **settings)

    async def indexed(self, *skills: StoredSkill) -> None:
        for skill in skills:
            assert await self.index.index(skill)

    async def settled(self) -> None:
        await asyncio.gather(*self.index._tasks)

    def names(self, candidates: Candidates) -> list[str]:
        return [c.skill.name for c in candidates.found]


class TestWhatASkillSaysAboutItself:
    def test_it_is_what_it_is_for_and_how_it_is_asked_for(self) -> None:
        assert what_a_skill_says_about_itself(LAMP) == [
            "Turn a lamp on or off, or set how bright it is.",
            "turn on the living room lamp",
            "switch off the lamp",
            "accendi la luce",
        ]

    def test_a_skill_with_no_examples_says_what_it_is_for(self) -> None:
        bare = SkillManifest(name="bare", description="Does a thing.", effect=LAMP.effect)

        assert what_a_skill_says_about_itself(bare) == ["Does a thing."]


class TestWhoseSkills:
    def test_a_shared_device_uses_the_tenants(self) -> None:
        assert owners_of(shared()) == [Owner.of_subject(Subject.tenant(TENANT))]

    def test_a_person_uses_the_tenants_and_their_own(self) -> None:
        assert owners_of(person()) == [
            Owner.of_subject(Subject.tenant(TENANT)),
            Owner(kind=OwnerKind.USER, id=USER),
        ]


class TestFindingWhatWasAskedFor:
    async def test_the_skills_near_the_message_are_found_and_the_others_are_not(self) -> None:
        rig = Rig()
        lamp = await enable(rig.store, LAMP)
        weather = await enable(rig.store, WEATHER)
        await rig.indexed(lamp, weather)

        found = await rig.matcher.candidates(shared(), "turn on the living room lamp")

        assert rig.names(found) == ["lamp"]
        assert found.degraded is False
        [candidate] = found.found
        assert candidate.score == pytest.approx(1.0)
        assert candidate.matched == "turn on the living room lamp"

    async def test_a_message_that_is_near_none_costs_no_model_at_all(self) -> None:
        rig = Rig()
        await rig.indexed(await enable(rig.store, LAMP), await enable(rig.store, WEATHER))

        found = await rig.matcher.candidates(shared(), "explain photosynthesis to me slowly")

        assert found == Candidates()

    async def test_the_nearest_come_first_and_only_as_many_as_are_wanted(self) -> None:
        rig = Rig(max_candidates=2, threshold=0.05)
        for manifest in (LAMP, WEATHER, VOLUME, ROUTER):
            await rig.indexed(await enable(rig.store, manifest))

        found = await rig.matcher.candidates(shared(), "set the volume to 30")

        assert len(found.found) == 2
        assert rig.names(found)[0] == "volume"
        scores = [c.score or 0.0 for c in found.found]
        assert scores == sorted(scores, reverse=True)

    async def test_a_skill_that_does_not_run_is_not_offered(self) -> None:
        rig = Rig()
        installed = await rig.store.install(
            TENANT,
            Owner.of_subject(Subject.tenant(TENANT)),
            LoadedSkill(LAMP, "", "---\nname: lamp\n---\n", {"scripts/run.py": b"x"}),
            now=START,
        )
        assert installed.created

        assert (await rig.matcher.candidates(shared(), "turn on the lamp")) == Candidates()
        assert rig.embedder.calls == []  # not even the message was embedded: nothing runs

    async def test_the_message_is_embedded_for_the_owner_of_the_skills(self) -> None:
        rig = Rig()
        await rig.indexed(await enable(rig.store, LAMP))

        await rig.matcher.candidates(shared(), "turn on the lamp")

        assert rig.embedder.calls[-1] == (
            Owner.of_subject(Subject.tenant(TENANT)),
            ("turn on the lamp",),
        )


class TestWhoseSkillsAreFound:
    async def test_a_person_finds_their_own_and_the_tenants_and_a_shared_device_only_the_tenants(
        self,
    ) -> None:
        rig = Rig()
        mine = Owner(kind=OwnerKind.USER, id=USER)
        await rig.indexed(await enable(rig.store, LAMP))
        await rig.indexed(await enable(rig.store, WEATHER, owner=mine))

        for_person = await rig.matcher.candidates(person(), "turn on the lamp what is the weather")
        for_device = await rig.matcher.candidates(shared(), "turn on the lamp what is the weather")

        assert sorted(rig.names(for_person)) == ["lamp", "weather"]
        assert rig.names(for_device) == ["lamp"]

    async def test_nobody_finds_the_skills_of_another_person_or_of_another_tenant(self) -> None:
        rig = Rig()
        somebody_else = Owner(kind=OwnerKind.USER, id=make_user_id())
        stranger = make_tenant_id()
        await rig.indexed(await enable(rig.store, LAMP, owner=somebody_else))
        await rig.indexed(await enable(rig.store, LAMP, tenant=stranger))

        assert (await rig.matcher.candidates(person(), "turn on the lamp")) == Candidates()
        assert (await rig.matcher.candidates(shared(), "turn on the lamp")) == Candidates()


class TestASkillThatIsNotIndexedYet:
    async def test_it_is_offered_without_a_score_and_is_indexed_for_the_next_message(self) -> None:
        rig = Rig()
        await enable(rig.store, LAMP)

        first = await rig.matcher.candidates(shared(), "turn on the lamp")
        await rig.settled()
        second = await rig.matcher.candidates(shared(), "turn on the lamp")

        assert [(c.skill.name, c.score) for c in first.found] == [("lamp", None)]
        assert [c.skill.name for c in second.found] == ["lamp"]
        assert (second.found[0].score or 0) > 0.8

    async def test_the_scored_come_before_the_unscored_and_the_unscored_are_by_name(self) -> None:
        rig = Rig()
        await rig.indexed(await enable(rig.store, LAMP))
        await enable(rig.store, WEATHER)
        await enable(rig.store, ROUTER)

        found = await rig.matcher.candidates(shared(), "turn on the lamp")

        assert rig.names(found) == ["lamp", "restart-router", "weather"]
        assert [c.score is None for c in found.found] == [False, True, True]

    async def test_no_more_of_them_are_offered_than_the_limit(self) -> None:
        rig = Rig(max_unscored=1)
        for manifest in (LAMP, WEATHER, VOLUME):
            await enable(rig.store, manifest)

        found = await rig.matcher.candidates(shared(), "hello")

        assert rig.names(found) == ["lamp"]

    async def test_it_is_not_indexed_twice_at_once(self) -> None:
        rig = Rig()
        await enable(rig.store, LAMP)

        await rig.matcher.candidates(shared(), "one")
        await rig.matcher.candidates(shared(), "two")
        await rig.settled()

        indexing = [call for call in rig.embedder.calls if len(call[1]) > 1]
        assert len(indexing) == 1


class TestWhenTheEmbeddingModelIsAway:
    @pytest.mark.parametrize(
        "failure",
        [
            EmbeddingNotConfiguredError("the owner has no embedding model"),
            GatewayUnavailableError("the model gateway could not be reached"),
        ],
    )
    async def test_the_skills_are_offered_unscored_and_it_is_said(self, failure: Exception) -> None:
        rig = Rig()
        await rig.indexed(await enable(rig.store, LAMP))
        await enable(rig.store, WEATHER)
        rig.embedder.failure = failure

        found = await rig.matcher.candidates(shared(), "turn on the lamp")

        assert rig.names(found) == ["lamp", "weather"]
        assert all(c.score is None for c in found.found)
        assert found.degraded is True

    async def test_a_model_that_does_not_answer_in_time_is_the_same(self) -> None:
        rig = Rig(timeout_s=0.05)
        rig.embedder.delay_s = 5
        await enable(rig.store, LAMP)

        found = await rig.matcher.candidates(shared(), "turn on the lamp")

        assert found.degraded is True
        assert rig.names(found) == ["lamp"]

    async def test_a_skill_that_could_not_be_indexed_is_no_worse_off(self) -> None:
        rig = Rig()
        skill = await enable(rig.store, LAMP)
        rig.embedder.failure = GatewayUnavailableError("away")

        assert await rig.index.index(skill) is False
        assert await rig.store.indexed(TENANT, skill.owner, "space-a") == set()

    async def test_a_skill_that_does_not_run_is_not_indexed(self) -> None:
        rig = Rig()
        installed = await rig.store.install(
            TENANT,
            Owner.of_subject(Subject.tenant(TENANT)),
            LoadedSkill(WEATHER, "", "---\nname: weather\n---\n", {"scripts/run.py": b"x"}),
            now=START,
        )
        stored = await rig.store.get_skill(TENANT, installed.skill_id)
        assert stored is not None

        assert await rig.index.index(stored) is False
        assert rig.embedder.calls == []


class TestWhenTheEmbeddingModelChanges:
    async def test_the_skills_are_offered_unscored_and_indexed_again_in_the_new_space(self) -> None:
        rig = Rig()
        lamp = await enable(rig.store, LAMP)
        await rig.indexed(lamp)
        rig.embedder.space_id = "another-model"

        first = await rig.matcher.candidates(shared(), "turn on the lamp")
        await rig.settled()
        second = await rig.matcher.candidates(shared(), "turn on the lamp")

        assert [c.score for c in first.found] == [None]
        assert (second.found[0].score or 0) > 0.8
        assert await rig.store.indexed(TENANT, lamp.owner, "space-a") == set()
        assert await rig.store.indexed(TENANT, lamp.owner, "another-model") == {lamp.id}


class TestClosing:
    async def test_what_is_being_indexed_is_stopped(self) -> None:
        rig = Rig()
        rig.embedder.delay_s = 30
        rig.index = SkillIndex(rig.store, rig.embedder, timeout_s=60)
        skill = await enable(rig.store, LAMP)

        rig.index.later([skill])
        await asyncio.sleep(0)
        started = list(rig.index._tasks)
        await rig.index.aclose()

        assert len(started) == 1
        assert started[0].cancelled()


def test_a_skill_id_is_not_needed_to_tell_two_owners_apart() -> None:
    assert Owner.of_subject(Subject.tenant(TENANT)) != Owner(kind=OwnerKind.USER, id=uuid4())
