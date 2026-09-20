from datetime import UTC, datetime
from uuid import uuid4

import pytest

from factotum.contracts.errors import CollectionNotFoundError, PolicyViolationError
from factotum.contracts.identity import Owner, OwnerKind, Role
from factotum.contracts.ids import CollectionId, TenantId
from factotum.contracts.knowledge import Collection, CollectionOwner
from factotum.contracts.testing import (
    make_device_id,
    make_tenant_id,
    make_tenant_principal,
    make_user_principal,
)
from factotum.knowledge.policy import (
    can_manage,
    can_read,
    manageable,
    owner_for,
    readable,
)

ADMIN = frozenset({Role.ADMIN})
MEMBER = frozenset({Role.MEMBER})


def collection_of(owner: Owner, tenant_id: TenantId) -> Collection:
    now = datetime.now(UTC)
    return Collection(
        id=CollectionId(uuid4()),
        tenant_id=tenant_id,
        owner=owner,
        name="Manuals",
        space_id="emb-x",
        dimensions=4,
        created_at=now,
        updated_at=now,
    )


class TestSeeing:
    def test_a_user_sees_their_own_collection(self) -> None:
        alice = make_user_principal()
        mine = collection_of(Owner.of_subject(alice.acts_as), alice.tenant_id)

        assert can_read(alice, mine)

    def test_a_user_does_not_see_the_collection_of_another_user(self) -> None:
        alice = make_user_principal()
        bob = make_user_principal(tenant_id=alice.tenant_id)
        hers = collection_of(Owner.of_subject(alice.acts_as), alice.tenant_id)

        assert not can_read(bob, hers)

    def test_everybody_in_the_tenant_sees_a_collection_of_the_tenant(self) -> None:
        alice = make_user_principal()
        bob = make_user_principal(tenant_id=alice.tenant_id)
        shared_device = make_tenant_principal(tenant_id=alice.tenant_id)
        shared = collection_of(Owner(kind=OwnerKind.TENANT, id=alice.tenant_id), alice.tenant_id)

        assert can_read(alice, shared)
        assert can_read(bob, shared)
        assert can_read(shared_device, shared)

    def test_a_shared_device_does_not_see_what_belongs_to_a_person(self) -> None:
        alice = make_user_principal()
        shared_device = make_tenant_principal(tenant_id=alice.tenant_id)
        hers = collection_of(Owner.of_subject(alice.acts_as), alice.tenant_id)

        assert not can_read(shared_device, hers)

    def test_nothing_crosses_a_tenant(self) -> None:
        alice = make_user_principal()
        elsewhere = make_tenant_id()
        theirs = collection_of(Owner(kind=OwnerKind.TENANT, id=elsewhere), elsewhere)

        assert not can_read(alice, theirs)


class TestChanging:
    def test_a_user_changes_their_own_collection(self) -> None:
        alice = make_user_principal(roles=MEMBER)
        mine = collection_of(Owner.of_subject(alice.acts_as), alice.tenant_id)

        assert can_manage(alice, mine)

    def test_only_an_administrator_changes_the_collection_of_the_tenant(self) -> None:
        admin = make_user_principal(roles=ADMIN)
        member = make_user_principal(tenant_id=admin.tenant_id, roles=MEMBER)
        shared = collection_of(Owner(kind=OwnerKind.TENANT, id=admin.tenant_id), admin.tenant_id)

        assert can_manage(admin, shared)
        assert not can_manage(member, shared)

    def test_a_shared_device_never_changes_anything(self) -> None:
        shared_device = make_tenant_principal()
        shared = collection_of(
            Owner(kind=OwnerKind.TENANT, id=shared_device.tenant_id), shared_device.tenant_id
        )

        assert can_read(shared_device, shared)
        assert not can_manage(shared_device, shared)

    def test_what_a_user_may_not_see_they_may_not_change(self) -> None:
        alice = make_user_principal(roles=ADMIN)
        bob = make_user_principal(tenant_id=alice.tenant_id, roles=ADMIN)
        hers = collection_of(Owner.of_subject(alice.acts_as), alice.tenant_id)

        assert not can_manage(bob, hers)

    def test_a_collection_of_a_device_is_not_changed_by_anybody_here(self) -> None:
        alice = make_user_principal(roles=ADMIN, device_id=make_device_id())
        device_owned = collection_of(
            Owner(kind=OwnerKind.DEVICE, id=alice.device_id or make_device_id()), alice.tenant_id
        )

        assert not can_manage(alice, device_owned)


class TestCreating:
    def test_a_user_creates_a_collection_of_their_own(self) -> None:
        alice = make_user_principal(roles=MEMBER)

        assert owner_for(alice, CollectionOwner.USER) == Owner.of_subject(alice.acts_as)

    def test_an_administrator_creates_one_for_the_tenant(self) -> None:
        admin = make_user_principal(roles=ADMIN)

        assert owner_for(admin, CollectionOwner.TENANT) == Owner(
            kind=OwnerKind.TENANT, id=admin.tenant_id
        )

    def test_a_member_does_not(self) -> None:
        member = make_user_principal(roles=MEMBER)

        with pytest.raises(PolicyViolationError, match="administrator"):
            owner_for(member, CollectionOwner.TENANT)

    @pytest.mark.parametrize("kind", list(CollectionOwner))
    def test_a_shared_device_creates_nothing(self, kind: CollectionOwner) -> None:
        with pytest.raises(PolicyViolationError, match="only a person"):
            owner_for(make_tenant_principal(), kind)


class TestTellingNothing:
    def test_a_collection_that_may_not_be_seen_is_not_found(self) -> None:
        alice = make_user_principal()
        bob = make_user_principal(tenant_id=alice.tenant_id, roles=ADMIN)
        hers = collection_of(Owner.of_subject(alice.acts_as), alice.tenant_id)

        with pytest.raises(CollectionNotFoundError):
            readable(bob, hers)
        with pytest.raises(CollectionNotFoundError):
            manageable(bob, hers)

    def test_a_collection_that_may_be_seen_and_not_changed_says_so(self) -> None:
        member = make_user_principal(roles=MEMBER)
        shared = collection_of(Owner(kind=OwnerKind.TENANT, id=member.tenant_id), member.tenant_id)

        assert readable(member, shared) is shared
        with pytest.raises(PolicyViolationError, match="but not change"):
            manageable(member, shared)

    def test_a_collection_that_may_be_changed_is_returned(self) -> None:
        alice = make_user_principal()
        mine = collection_of(Owner.of_subject(alice.acts_as), alice.tenant_id)

        assert manageable(alice, mine) is mine
