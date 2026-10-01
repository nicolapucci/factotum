from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from factotum.actions.directory import (
    FileDeviceDirectory,
    HubDeviceDirectory,
    load_directory,
    named,
)
from factotum.common.config import Environment, ServiceSettings
from factotum.common.devauth import DevPrincipalDelegation
from factotum.contracts.devices import ExternalDeviceRecord, JsonScalar, SystemDeviceRecord
from factotum.contracts.errors import AuthError
from factotum.contracts.hub import BindingList, BindingView, DeviceList, SystemDeviceWithCredential
from factotum.contracts.identity import Owner, Principal, Role, Subject
from factotum.contracts.ids import BindingId, DeviceId, TenantId
from factotum.contracts.testing import (
    make_external_device,
    make_system_device,
    make_tenant_id,
    make_user_id,
)
from factotum.devices.home import parse_home

TEST = ServiceSettings(_env_file=None, environment=Environment.TEST)
TENANT = make_tenant_id()
USER = make_user_id()
EXAMPLE = Path(__file__).resolve().parents[3] / "config" / "devices.example.yaml"

HOME = """
devices:
  - name: Kitchen speaker
    hardware_id: kitchen-01
    token: kitchen-credential
    owner: tenant
    capabilities: [audio_in, audio_out]
  - name: My phone
    hardware_id: phone-01
    token: phone-credential
    owner: user
    capabilities: [audio_in, audio_out]
  - kind: external
    name: Living room lamp
    aliases: [lamp, the lamp]
  - kind: external
    name: Bedroom lamp
    owner: user
    aliases: [My Lamp]
  - kind: external
    name: Garage door
    sensitivity: irreversible
bindings:
  - skill: lamp
    device: Living room lamp
    params: {entity_id: light.living_room}
  - skill: lamp
    device: Bedroom lamp
    params: {entity_id: light.bedroom}
  - skill: garage-door
    device: Garage door
    params: {entity_id: cover.garage}
  - skill: lamp
    device: Garage door
    enabled: false
"""


def directory(text: str = HOME) -> FileDeviceDirectory:
    home = parse_home(text, tenant_id=TENANT, user_id=USER, source="home")
    return FileDeviceDirectory(TEST, home)


def shared() -> Principal:
    return Principal(tenant_id=TENANT, acts_as=Subject.tenant(TENANT))


def person() -> Principal:
    return Principal(tenant_id=TENANT, acts_as=Subject.user(USER), roles=frozenset({Role.MEMBER}))


def stranger() -> Principal:
    other = make_tenant_id()
    return Principal(tenant_id=other, acts_as=Subject.tenant(other))


class TestWhoMayUseWhat:
    async def test_a_shared_device_uses_what_is_the_tenants_and_not_what_is_a_persons_own(
        self,
    ) -> None:
        d = directory()

        systems = await d.system_devices(shared())
        lamps = await d.bound_devices(shared(), "lamp")

        assert [s.name for s in systems] == ["Kitchen speaker"]
        assert [b.device.name for b in lamps] == ["Living room lamp"]

    async def test_a_person_uses_the_tenants_and_their_own(self) -> None:
        d = directory()

        assert [s.name for s in await d.system_devices(person())] == [
            "Kitchen speaker",
            "My phone",
        ]
        assert [b.device.name for b in await d.bound_devices(person(), "lamp")] == [
            "Bedroom lamp",
            "Living room lamp",
        ]

    async def test_another_tenant_uses_nothing_of_this_one(self) -> None:
        d = directory()

        assert await d.system_devices(stranger()) == []
        assert await d.bound_devices(stranger(), "lamp") == []

    async def test_a_system_device_is_found_by_its_id_in_its_own_tenant_only(self) -> None:
        d = directory()
        [(kitchen, _), *_] = parse_home(HOME, tenant_id=TENANT, user_id=USER, source="home").system

        assert await d.system_device(TENANT, kitchen.id) == kitchen
        assert await d.system_device(make_tenant_id(), kitchen.id) is None

    async def test_a_device_is_known_when_it_is_somebodys_even_if_the_caller_may_not_use_it(
        self,
    ) -> None:
        # what is asked of the directory by id is what the caller's own device says it is: the
        # device that spoke is known whoever it acts for
        d = directory()
        phone = next(
            r
            for r, _ in parse_home(HOME, tenant_id=TENANT, user_id=USER, source="home").system
            if r.name == "My phone"
        )

        assert await d.system_device(TENANT, phone.id) == phone


class TestTheBindings:
    async def test_a_skill_is_bound_to_devices_with_the_devices_own_parameters(self) -> None:
        d = directory()

        [lamp] = await d.bound_devices(shared(), "lamp")

        assert lamp.params == {"entity_id": "light.living_room"}

    async def test_a_skill_that_is_bound_to_nothing_has_no_devices(self) -> None:
        assert await directory().bound_devices(person(), "weather") == []

    async def test_a_binding_that_is_switched_off_is_not_there(self) -> None:
        names = [b.device.name for b in await directory().bound_devices(person(), "lamp")]

        assert "Garage door" not in names

    async def test_what_is_bound_is_told_the_way_the_file_says_it(self) -> None:
        [door] = await directory().bound_devices(shared(), "garage-door")

        assert door.device.name == "Garage door"
        assert door.params == {"entity_id": "cover.garage"}


class TestNamedDevices:
    async def test_a_device_is_called_by_its_name_or_by_an_alias_in_any_case(self) -> None:
        devices = [b.device for b in await directory().bound_devices(person(), "lamp")]

        assert [d.name for d in named(devices, "living room lamp")] == ["Living room lamp"]
        assert [d.name for d in named(devices, "THE LAMP")] == ["Living room lamp"]
        assert [d.name for d in named(devices, "  my   lamp ")] == ["Bedroom lamp"]

    async def test_a_name_that_nobody_has_is_nobody(self) -> None:
        devices = [b.device for b in await directory().bound_devices(person(), "lamp")]

        assert named(devices, "the stove") == []
        assert named(devices, "") == []

    async def test_a_name_that_two_have_is_both_so_that_it_can_be_told_it_is_unclear(self) -> None:
        text = HOME.replace("aliases: [My Lamp]", "aliases: [lamp]")
        devices = [b.device for b in await directory(text).bound_devices(person(), "lamp")]

        assert sorted(d.name for d in named(devices, "lamp")) == [
            "Bedroom lamp",
            "Living room lamp",
        ]


class TestWhereItIsAllowed:
    @pytest.mark.parametrize("environment", [Environment.PRODUCTION])
    def test_it_does_not_exist_in_production(self, environment: Environment) -> None:
        settings = ServiceSettings(_env_file=None, environment=environment)

        with pytest.raises(AuthError, match="disabled outside the development and test"):
            FileDeviceDirectory(settings, parse_home(HOME, tenant_id=TENANT, user_id=USER))

    @pytest.mark.parametrize("environment", [Environment.DEVELOPMENT, Environment.TEST])
    def test_it_does_in_development_and_in_tests(self, environment: Environment) -> None:
        settings = ServiceSettings(_env_file=None, environment=environment)

        assert FileDeviceDirectory(settings, parse_home(HOME, tenant_id=TENANT, user_id=USER))


class TestTheExampleFile:
    async def test_the_file_the_documentation_says_to_copy_is_a_directory(self) -> None:
        d = load_directory(EXAMPLE, TEST, tenant_id=TENANT, user_id=USER)

        lamps = await d.bound_devices(person(), "lamp")
        doors = await d.bound_devices(shared(), "garage-door")

        assert [b.device.name for b in lamps] == ["Bedroom lamp", "Living room lamp"]
        assert [b.device.name for b in doors] == ["Garage door"]

    def test_a_file_that_is_not_there_is_an_error_that_names_it(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match=r"nowhere\.yaml"):
            load_directory(tmp_path / "nowhere.yaml", TEST, tenant_id=TENANT, user_id=USER)

    def test_a_file_that_is_not_a_home_says_where(self, tmp_path: Path) -> None:
        path = tmp_path / "home.yaml"
        path.write_text("devices: 3", encoding="utf-8")

        with pytest.raises(ValueError, match=r"home\.yaml: expected a mapping"):
            load_directory(path, TEST, tenant_id=TENANT, user_id=USER)


class Hub:
    """A hub that records what it is asked and answers with the devices and bindings it is told
    to."""

    def __init__(self) -> None:
        self.sent: list[httpx.Request] = []
        self.devices = DeviceList()
        self.bindings = BindingList()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        body = self.devices if request.url.path == "/v1/devices" else self.bindings
        return httpx.Response(200, content=body.model_dump_json())

    def directory(self, tenant_id: TenantId = TENANT) -> HubDeviceDirectory:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self), base_url="http://hub.test")
        return HubDeviceDirectory(client, DevPrincipalDelegation(TEST), tenant_id)


def with_credential(device: SystemDeviceRecord) -> SystemDeviceWithCredential:
    return SystemDeviceWithCredential(device=device, credential_hash="irrelevant-hash")


def binding(
    device_id: DeviceId,
    *,
    skill: str,
    params: dict[str, JsonScalar] | None = None,
    enabled: bool = True,
) -> BindingView:
    return BindingView(
        id=BindingId(uuid4()),
        skill=skill,
        device_id=device_id,
        params=params or {},
        enabled=enabled,
    )


Devices = tuple[
    SystemDeviceRecord,
    SystemDeviceRecord,
    ExternalDeviceRecord,
    ExternalDeviceRecord,
    ExternalDeviceRecord,
]


def devices() -> Devices:
    kitchen = make_system_device(
        tenant_id=TENANT, name="Kitchen speaker", owner=Owner.of_subject(Subject.tenant(TENANT))
    )
    phone = make_system_device(
        tenant_id=TENANT, name="My phone", owner=Owner.of_subject(Subject.user(USER))
    )
    living_room_lamp = make_external_device(
        tenant_id=TENANT, name="Living room lamp", owner=Owner.of_subject(Subject.tenant(TENANT))
    )
    bedroom_lamp = make_external_device(
        tenant_id=TENANT, name="Bedroom lamp", owner=Owner.of_subject(Subject.user(USER))
    )
    garage_door = make_external_device(
        tenant_id=TENANT, name="Garage door", owner=Owner.of_subject(Subject.tenant(TENANT))
    )
    return kitchen, phone, living_room_lamp, bedroom_lamp, garage_door


async def loaded(hub: Hub | None = None) -> tuple[HubDeviceDirectory, Hub]:
    hub = hub if hub is not None else Hub()
    kitchen, phone, living_room_lamp, bedroom_lamp, garage_door = devices()
    hub.devices = DeviceList(
        system=(with_credential(kitchen), with_credential(phone)),
        external=(living_room_lamp, bedroom_lamp, garage_door),
    )
    hub.bindings = BindingList(
        bindings=(
            binding(living_room_lamp.id, skill="lamp", params={"entity_id": "light.living_room"}),
            binding(bedroom_lamp.id, skill="lamp", params={"entity_id": "light.bedroom"}),
            binding(garage_door.id, skill="garage-door", params={"entity_id": "cover.garage"}),
            binding(garage_door.id, skill="lamp", enabled=False),
        )
    )
    directory = hub.directory()
    await directory.reload()
    return directory, hub


class TestTheHubDirectory:
    async def test_it_knows_nothing_before_it_is_loaded(self) -> None:
        d = Hub().directory()

        assert await d.system_devices(shared()) == []
        assert await d.bound_devices(shared(), "lamp") == []

    async def test_reload_asks_the_hub_for_devices_and_bindings_as_the_tenant(self) -> None:
        hub = Hub()
        d = hub.directory()

        await d.reload()

        paths = {request.url.path for request in hub.sent}
        assert paths == {"/v1/devices", "/v1/bindings"}
        assert all(request.headers["x-factotum-dev-subject"] == "tenant" for request in hub.sent)

    async def test_a_shared_device_uses_what_is_the_tenants_and_not_what_is_a_persons_own(
        self,
    ) -> None:
        d, _ = await loaded()

        systems = await d.system_devices(shared())
        lamps = await d.bound_devices(shared(), "lamp")

        assert [s.name for s in systems] == ["Kitchen speaker"]
        assert [b.device.name for b in lamps] == ["Living room lamp"]

    async def test_a_person_uses_the_tenants_and_their_own(self) -> None:
        d, _ = await loaded()

        assert [s.name for s in await d.system_devices(person())] == [
            "Kitchen speaker",
            "My phone",
        ]
        assert [b.device.name for b in await d.bound_devices(person(), "lamp")] == [
            "Bedroom lamp",
            "Living room lamp",
        ]

    async def test_another_tenant_uses_nothing_of_this_one(self) -> None:
        d, _ = await loaded()

        assert await d.system_devices(stranger()) == []
        assert await d.bound_devices(stranger(), "lamp") == []

    async def test_a_system_device_is_found_by_its_id_in_its_own_tenant_only(self) -> None:
        d, hub = await loaded()
        kitchen = hub.devices.system[0].device

        assert await d.system_device(TENANT, kitchen.id) == kitchen
        assert await d.system_device(make_tenant_id(), kitchen.id) is None

    async def test_a_binding_that_is_switched_off_is_not_there(self) -> None:
        d, _ = await loaded()

        names = [b.device.name for b in await d.bound_devices(person(), "lamp")]

        assert "Garage door" not in names

    async def test_what_is_bound_carries_the_devices_own_parameters(self) -> None:
        d, _ = await loaded()

        [door] = await d.bound_devices(shared(), "garage-door")

        assert door.params == {"entity_id": "cover.garage"}

    async def test_a_skill_that_is_bound_to_nothing_has_no_devices(self) -> None:
        d, _ = await loaded()

        assert await d.bound_devices(person(), "weather") == []

    async def test_reloading_again_replaces_what_was_there_before(self) -> None:
        d, hub = await loaded()

        hub.devices = DeviceList()
        hub.bindings = BindingList()
        await d.reload()

        assert await d.system_devices(shared()) == []
        assert await d.bound_devices(shared(), "lamp") == []

    async def test_a_hub_that_answers_with_an_error_is_not_swallowed(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(refuse), base_url="http://hub.test"
        )
        d = HubDeviceDirectory(client, DevPrincipalDelegation(TEST), TENANT)

        with pytest.raises(httpx.HTTPStatusError):
            await d.reload()
