"""The behaviour every ``HubStore`` must have: both ``InMemoryStore`` and ``PostgresStore`` are run
against these tests. The database of a module is shared by its tests (ADR-less convention of every
other service's store contract), so every test makes its own tenant and, where a username is looked
up across every tenant (``find_user_by_username``), its own unique one too.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from factotum.contracts.devices import ExternalDeviceRecord, JsonScalar
from factotum.contracts.identity import Role
from factotum.contracts.ids import BindingId, DeviceId, TenantId
from factotum.contracts.testing import (
    make_device_id,
    make_external_device,
    make_system_device,
    make_tenant_id,
    make_user_id,
)
from factotum.hub.store import HubStore, StoredBinding, StoredDevice, StoredUser

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


async def _tenant(store: HubStore) -> TenantId:
    """A tenant that really exists, for the tests of what belongs to one (a device's tenant_id is
    a foreign key: unlike a user, a device is never created for a tenant that was only imagined)."""
    tenant_id = make_tenant_id()
    await store.bootstrap(tenant_id, _user(tenant_id))
    return tenant_id


def _system(
    tenant_id: TenantId,
    *,
    name: str = "Kitchen speaker",
    hardware_id: str = "AA:BB:CC:DD:EE:FF",
    created_at: datetime | None = None,
) -> StoredDevice:
    return StoredDevice(
        record=make_system_device(tenant_id=tenant_id, name=name, hardware_id=hardware_id),
        credential_hash="irrelevant-hash",
        created_at=created_at or datetime.now(UTC),
    )


def _external(
    tenant_id: TenantId, *, name: str = "Kitchen light", created_at: datetime | None = None
) -> StoredDevice:
    return StoredDevice(
        record=make_external_device(tenant_id=tenant_id, name=name),
        credential_hash=None,
        created_at=created_at or datetime.now(UTC),
    )


async def test_a_system_device_is_added_with_its_credential_hash(store: HubStore) -> None:
    tenant_id = await _tenant(store)
    device = _system(tenant_id)

    assert await store.create_device(device) is True
    assert await store.get_device(tenant_id, device.record.id) == device


async def test_an_external_device_is_added_with_no_credential(store: HubStore) -> None:
    tenant_id = await _tenant(store)
    device = _external(tenant_id)

    assert await store.create_device(device) is True
    stored = await store.get_device(tenant_id, device.record.id)
    assert stored is not None
    assert stored.credential_hash is None
    assert isinstance(stored.record, ExternalDeviceRecord)


async def test_a_device_is_not_found_through_another_tenant(store: HubStore) -> None:
    tenant_id = await _tenant(store)
    device = _system(tenant_id)
    await store.create_device(device)

    assert await store.get_device(make_tenant_id(), device.record.id) is None


async def test_an_unknown_device_is_not_found(store: HubStore) -> None:
    assert await store.get_device(make_tenant_id(), make_device_id()) is None


async def test_a_duplicate_name_in_the_same_tenant_is_refused(store: HubStore) -> None:
    tenant_id = await _tenant(store)
    await store.create_device(_system(tenant_id, name="same name", hardware_id="one"))

    added = await store.create_device(_system(tenant_id, name="same name", hardware_id="two"))

    assert added is False


async def test_a_duplicate_hardware_id_in_the_same_tenant_is_refused(store: HubStore) -> None:
    tenant_id = await _tenant(store)
    await store.create_device(_system(tenant_id, name="one name", hardware_id="same-hw"))

    added = await store.create_device(
        _system(tenant_id, name="another name", hardware_id="same-hw")
    )

    assert added is False


async def test_the_same_name_and_hardware_id_are_free_in_another_tenant(store: HubStore) -> None:
    first_tenant = await _tenant(store)
    await store.create_device(_system(first_tenant, name="shared", hardware_id="shared-hw"))
    second_tenant = await _tenant(store)

    added = await store.create_device(
        _system(second_tenant, name="shared", hardware_id="shared-hw")
    )

    assert added is True


async def test_devices_are_listed_earliest_first(store: HubStore) -> None:
    tenant_id = await _tenant(store)
    first = _system(tenant_id, name="first", created_at=datetime(2026, 1, 1, tzinfo=UTC))
    await store.create_device(first)
    second = _external(tenant_id, name="second", created_at=datetime(2026, 1, 2, tzinfo=UTC))
    await store.create_device(second)

    listed = await store.list_devices(tenant_id)

    assert [d.record.name for d in listed] == ["first", "second"]


async def test_listing_another_tenants_devices_sees_nothing(store: HubStore) -> None:
    tenant_id = await _tenant(store)
    await store.create_device(_system(tenant_id))

    assert await store.list_devices(make_tenant_id()) == []


def _binding(
    tenant_id: TenantId,
    device_id: DeviceId,
    *,
    skill: str = "lamp",
    params: dict[str, JsonScalar] | None = None,
    enabled: bool = True,
    created_at: datetime | None = None,
) -> StoredBinding:
    return StoredBinding(
        id=BindingId(uuid4()),
        tenant_id=tenant_id,
        skill=skill,
        device_id=device_id,
        params=params or {},
        enabled=enabled,
        created_at=created_at or datetime.now(UTC),
    )


async def test_a_binding_is_added_and_listed(store: HubStore) -> None:
    tenant_id = await _tenant(store)
    device = _external(tenant_id)
    await store.create_device(device)
    binding = _binding(tenant_id, device.record.id, params={"entity_id": "light.living_room"})

    assert await store.create_binding(binding) is True
    assert await store.list_bindings(tenant_id) == [binding]


async def test_the_same_skill_cannot_be_bound_to_the_same_device_twice(store: HubStore) -> None:
    tenant_id = await _tenant(store)
    device = _external(tenant_id)
    await store.create_device(device)
    await store.create_binding(_binding(tenant_id, device.record.id, skill="lamp"))

    added = await store.create_binding(
        _binding(tenant_id, device.record.id, skill="lamp", params={"different": "params"})
    )

    assert added is False


async def test_two_different_skills_can_be_bound_to_the_same_device(store: HubStore) -> None:
    tenant_id = await _tenant(store)
    device = _external(tenant_id)
    await store.create_device(device)
    await store.create_binding(_binding(tenant_id, device.record.id, skill="lamp"))

    added = await store.create_binding(_binding(tenant_id, device.record.id, skill="dimmer"))

    assert added is True
    assert len(await store.list_bindings(tenant_id)) == 2


async def test_bindings_are_listed_earliest_first(store: HubStore) -> None:
    tenant_id = await _tenant(store)
    device = _external(tenant_id)
    await store.create_device(device)
    first = _binding(
        tenant_id,
        device.record.id,
        skill="first-skill",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    await store.create_binding(first)
    second = _binding(
        tenant_id,
        device.record.id,
        skill="second-skill",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    await store.create_binding(second)

    listed = await store.list_bindings(tenant_id)

    assert [b.skill for b in listed] == ["first-skill", "second-skill"]


async def test_listing_another_tenants_bindings_sees_nothing(store: HubStore) -> None:
    tenant_id = await _tenant(store)
    device = _external(tenant_id)
    await store.create_device(device)
    await store.create_binding(_binding(tenant_id, device.record.id))

    assert await store.list_bindings(make_tenant_id()) == []
