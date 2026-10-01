"""The behaviour every ``HubStore`` must have: both ``InMemoryStore`` and ``PostgresStore`` are run
against these tests. The database of a module is shared by its tests (ADR-less convention of every
other service's store contract), so every test makes its own tenant and, where a username is looked
up across every tenant (``find_user_by_username``), its own unique one too.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from factotum.contracts.identity import Role
from factotum.contracts.ids import TenantId
from factotum.contracts.testing import make_tenant_id, make_user_id
from factotum.hub.store import HubStore, StoredUser

pytestmark = pytest.mark.asyncio(loop_scope="module")


def _name(label: str = "user") -> str:
    return f"{label}-{uuid4().hex[:10]}"


def _user(
    tenant_id: TenantId,
    *,
    username: str | None = None,
    roles: frozenset[Role] = frozenset({Role.ADMIN}),
    must_change_password: bool = True,
    created_at: datetime | None = None,
) -> StoredUser:
    return StoredUser(
        id=make_user_id(),
        tenant_id=tenant_id,
        username=username or _name(),
        password_hash="irrelevant-hash",
        roles=roles,
        must_change_password=must_change_password,
        created_at=created_at or datetime.now(UTC),
    )


async def test_bootstrap_adds_one_tenant(store: HubStore) -> None:
    before = await store.tenant_count()
    tenant_id = make_tenant_id()

    await store.bootstrap(tenant_id, _user(tenant_id))

    assert await store.tenant_count() == before + 1


async def test_the_administrator_of_a_bootstrap_is_found_by_id_and_by_username(
    store: HubStore,
) -> None:
    tenant_id = make_tenant_id()
    admin = _user(tenant_id)

    await store.bootstrap(tenant_id, admin)

    assert await store.get_user(tenant_id, admin.id) == admin
    assert await store.get_user_by_username(tenant_id, admin.username) == admin
    assert await store.find_user_by_username(admin.username) == admin


async def test_a_user_is_not_found_through_another_tenant(store: HubStore) -> None:
    tenant_id = make_tenant_id()
    admin = _user(tenant_id)
    await store.bootstrap(tenant_id, admin)

    assert await store.get_user(make_tenant_id(), admin.id) is None
    assert await store.get_user_by_username(make_tenant_id(), admin.username) is None


async def test_an_unknown_user_is_not_found(store: HubStore) -> None:
    assert await store.get_user(make_tenant_id(), make_user_id()) is None
    assert await store.get_user_by_username(make_tenant_id(), _name("nobody")) is None
    assert await store.find_user_by_username(_name("nobody")) is None


async def test_create_user_adds_a_user_of_the_tenant(store: HubStore) -> None:
    tenant_id = make_tenant_id()
    await store.bootstrap(tenant_id, _user(tenant_id))
    created = _user(tenant_id, roles=frozenset({Role.MEMBER}))

    stored = await store.create_user(created)

    assert stored == created
    assert await store.get_user(tenant_id, created.id) == created


async def test_a_duplicate_username_in_the_same_tenant_is_refused(store: HubStore) -> None:
    tenant_id = make_tenant_id()
    name = _name("dup")
    await store.bootstrap(tenant_id, _user(tenant_id, username=name))
    second = _user(tenant_id, username=name)

    stored = await store.create_user(second)

    assert stored is None
    assert await store.get_user(tenant_id, second.id) is None


async def test_the_same_username_is_free_in_another_tenant(store: HubStore) -> None:
    name = _name("shared")
    first_tenant = make_tenant_id()
    await store.bootstrap(first_tenant, _user(first_tenant, username=name))
    second_tenant = make_tenant_id()

    # A second tenant, of its own: a user is only ever added to one that already exists.
    await store.bootstrap(second_tenant, _user(second_tenant, username=name))

    assert await store.get_user_by_username(second_tenant, name) is not None


async def test_users_are_listed_earliest_first(store: HubStore) -> None:
    tenant_id = make_tenant_id()
    admin = _user(tenant_id, created_at=datetime(2026, 1, 1, tzinfo=UTC))
    await store.bootstrap(tenant_id, admin)
    later = _user(tenant_id, created_at=datetime(2026, 1, 2, tzinfo=UTC))
    await store.create_user(later)

    listed = await store.list_users(tenant_id)

    assert [user.username for user in listed] == [admin.username, later.username]


async def test_listing_another_tenants_users_sees_nothing(store: HubStore) -> None:
    tenant_id = make_tenant_id()
    await store.bootstrap(tenant_id, _user(tenant_id))

    assert await store.list_users(make_tenant_id()) == []


async def test_the_password_can_be_set_and_clears_the_flag(store: HubStore) -> None:
    tenant_id = make_tenant_id()
    admin = _user(tenant_id, must_change_password=True)
    await store.bootstrap(tenant_id, admin)

    changed = await store.set_password(tenant_id, admin.id, "new-hash")

    assert changed is True
    stored = await store.get_user(tenant_id, admin.id)
    assert stored is not None
    assert (stored.password_hash, stored.must_change_password) == ("new-hash", False)


async def test_setting_the_password_of_an_unknown_user_does_nothing(store: HubStore) -> None:
    assert await store.set_password(make_tenant_id(), make_user_id(), "new-hash") is False
