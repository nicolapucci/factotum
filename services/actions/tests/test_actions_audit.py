"""Reading the audit trail: by whom, and a page at a time."""

from datetime import timedelta
from uuid import uuid4

import pytest

from factotum.actions.audit import MAX_PAGE, AuditReader
from factotum.actions.rig import ActionsRig
from factotum.actions.testing import START
from factotum.contracts.audit import ActionResult, AuditRecord
from factotum.contracts.authorization import DecisionOutcome
from factotum.contracts.errors import PolicyViolationError
from factotum.contracts.identity import Principal, Role, Subject
from factotum.contracts.ids import AuditId, DeviceId, SkillId, TenantId, UserId


def record(
    tenant: TenantId,
    minutes: int,
    *,
    skill: str = "lamp",
    device: DeviceId | None = None,
) -> AuditRecord:
    return AuditRecord(
        id=AuditId(uuid4()),
        tenant_id=tenant,
        occurred_at=START + timedelta(minutes=minutes),
        device_id=device,
        acts_as=Subject.tenant(tenant),
        skill_id=SkillId(uuid4()),
        skill_name=skill,
        decision=DecisionOutcome.ALLOW,
        result=ActionResult.EXECUTED,
    )


async def keep(rig: ActionsRig, *records: AuditRecord) -> AuditReader:
    for one in records:
        await rig.store.add_audit(one)
    return AuditReader(rig.store)


class TestPaging:
    async def test_the_latest_come_first_and_a_page_says_where_the_next_begins(self) -> None:
        rig = ActionsRig()
        reader = await keep(rig, *(record(rig.tenant, minute) for minute in range(5)))

        first = await reader.list(rig.principal(None), limit=2)
        second = await reader.list(rig.principal(None), limit=2, before=first.next_before)
        third = await reader.list(rig.principal(None), limit=2, before=second.next_before)

        assert [r.occurred_at for r in first.records] == [
            START + timedelta(minutes=4),
            START + timedelta(minutes=3),
        ]
        assert first.next_before == START + timedelta(minutes=3)
        assert [r.occurred_at for r in second.records] == [
            START + timedelta(minutes=2),
            START + timedelta(minutes=1),
        ]
        assert [r.occurred_at for r in third.records] == [START]
        assert third.next_before is None

    async def test_a_page_that_ends_the_trail_exactly_does_not_promise_another(self) -> None:
        rig = ActionsRig()
        reader = await keep(rig, record(rig.tenant, 0), record(rig.tenant, 1))

        page = await reader.list(rig.principal(None), limit=2)

        assert len(page.records) == 2
        assert page.next_before is None

    async def test_an_empty_trail_is_an_empty_page(self) -> None:
        rig = ActionsRig()

        page = await AuditReader(rig.store).list(rig.principal(None))

        assert (page.records, page.next_before) == ((), None)

    @pytest.mark.parametrize(("asked", "given"), [(0, 1), (-5, 1), (10_000, MAX_PAGE)])
    async def test_a_page_is_never_empty_by_request_and_never_larger_than_the_most(
        self, asked: int, given: int
    ) -> None:
        rig = ActionsRig()
        reader = await keep(rig, *(record(rig.tenant, minute) for minute in range(MAX_PAGE + 2)))

        page = await reader.list(rig.principal(None), limit=asked)

        assert len(page.records) == given


class TestFiltering:
    async def test_by_skill_and_by_device(self) -> None:
        rig = ActionsRig()
        kitchen = rig.device("Kitchen speaker").id
        reader = await keep(
            rig,
            record(rig.tenant, 0, skill="lamp", device=kitchen),
            record(rig.tenant, 1, skill="garage-door", device=kitchen),
            record(rig.tenant, 2, skill="lamp"),
        )
        admin = rig.principal(None)

        lamps = await reader.list(admin, skill_name="lamp")
        from_kitchen = await reader.list(admin, device_id=kitchen)
        both = await reader.list(admin, skill_name="lamp", device_id=kitchen)

        assert [r.skill_name for r in lamps.records] == ["lamp", "lamp"]
        assert [r.skill_name for r in from_kitchen.records] == ["garage-door", "lamp"]
        assert len(both.records) == 1


class TestWhoMayRead:
    async def test_only_the_administrators_of_the_tenant(self) -> None:
        rig = ActionsRig()
        reader = await keep(rig, record(rig.tenant, 0))
        member = Principal(
            tenant_id=rig.tenant,
            acts_as=Subject.user(UserId(uuid4())),
            roles=frozenset({Role.MEMBER}),
        )

        with pytest.raises(PolicyViolationError, match="administrators"):
            await reader.list(member)

    async def test_never_through_a_device_not_even_an_administrators(self) -> None:
        rig = ActionsRig()
        reader = await keep(rig, record(rig.tenant, 0))
        admin_at_a_device = Principal(
            tenant_id=rig.tenant,
            device_id=rig.device("My phone").id,
            acts_as=Subject.user(rig.user),
            roles=frozenset({Role.ADMIN}),
        )

        for who in (rig.principal("Kitchen speaker"), rig.principal("My phone"), admin_at_a_device):
            with pytest.raises(PolicyViolationError, match="never through a device"):
                await reader.list(who)

    async def test_nothing_of_another_tenant_is_read(self) -> None:
        rig = ActionsRig()
        reader = await keep(rig, record(rig.tenant, 0))
        other = TenantId(uuid4())
        stranger = Principal(
            tenant_id=other, acts_as=Subject.user(UserId(uuid4())), roles=frozenset({Role.ADMIN})
        )

        assert (await reader.list(stranger)).records == ()
