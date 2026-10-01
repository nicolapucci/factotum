from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from factotum.common.config import Environment, ServiceSettings
from factotum.common.credentials import hash_credential, new_credential
from factotum.common.devauth import DevPrincipalDelegation
from factotum.contracts.capabilities import Capability, DisplayInfo
from factotum.contracts.effects import Effect
from factotum.contracts.errors import AuthError
from factotum.contracts.hub import DeviceList, SystemDeviceWithCredential
from factotum.contracts.identity import Owner, OwnerKind, Role, Subject, SubjectKind
from factotum.contracts.ids import DeviceId, TenantId
from factotum.contracts.testing import (
    make_device_id,
    make_system_device,
    make_tenant_id,
    make_user_id,
)
from factotum.edge.registry import (
    FileDeviceRegistry,
    HubDeviceRegistry,
    device_id_for,
    load_devices,
    load_devices_file,
    principal_of,
)

EXAMPLE = Path(__file__).resolve().parents[3] / "config" / "devices.example.yaml"

TEST = ServiceSettings(_env_file=None, environment=Environment.TEST)
TENANT = make_tenant_id()
USER = make_user_id()

DEVICES = """
devices:
  - name: Kitchen speaker
    hardware_id: kitchen-01
    token: kitchen-credential
    owner: tenant
    capabilities: [audio_in, audio_out, led]
  - name: Ada's phone
    hardware_id: phone-ada
    token: ada-credential-1
    owner: user
    capabilities: [audio_in, audio_out, display]
    display: {width: 400, height: 800, touch: true}
    tags: [phone]
    aliases: [ada]
"""


def load(text: str = DEVICES, settings: ServiceSettings = TEST) -> FileDeviceRegistry:
    return load_devices(text, settings, tenant_id=TENANT, user_id=USER)


def entry(**changes: object) -> str:
    fields: dict[str, object] = {
        "name": "Kitchen speaker",
        "hardware_id": "kitchen-01",
        "token": "kitchen-credential",
        "capabilities": "[audio_in]",
    }
    lines = "\n".join(f"    {key}: {value}" for key, value in {**fields, **changes}.items())
    return "devices:\n  -\n" + lines + "\n"


class TestWhoIsKnown:
    async def test_a_device_is_known_by_its_hardware_id_and_its_credential(self) -> None:
        registry = load()

        kitchen = await registry.authenticate("kitchen-01", "kitchen-credential")
        phone = await registry.authenticate("phone-ada", "ada-credential-1")

        assert kitchen is not None
        assert (kitchen.name, kitchen.capabilities) == (
            "Kitchen speaker",
            frozenset({Capability.AUDIO_IN, Capability.AUDIO_OUT, Capability.LED}),
        )
        assert phone is not None
        assert phone.display == DisplayInfo(width=400, height=800, touch=True)
        assert (phone.tags, phone.aliases) == (frozenset({"phone"}), ("ada",))

    @pytest.mark.parametrize(
        ("hardware_id", "token"),
        [
            ("kitchen-01", "not-the-credential"),
            ("kitchen-01", "ada-credential-1"),  # the credential of another device
            ("phone-ada", "kitchen-credential"),
            ("nobody-01", "kitchen-credential"),
            ("nobody-01", "x"),
            ("kitchen-01", ""),
        ],
    )
    async def test_anything_else_is_nobody(self, hardware_id: str, token: str) -> None:
        assert await load().authenticate(hardware_id, token) is None

    async def test_a_device_is_found_by_its_id_in_its_own_tenant_only(self) -> None:
        registry = load()
        kitchen = await registry.authenticate("kitchen-01", "kitchen-credential")
        assert kitchen is not None

        assert await registry.get(TENANT, kitchen.id) == kitchen
        assert await registry.get(make_tenant_id(), kitchen.id) is None
        assert await registry.get(TENANT, DeviceId(uuid4())) is None


class TestWhoTheyActFor:
    async def test_a_shared_device_acts_for_the_tenant_and_a_personal_one_for_its_owner(
        self,
    ) -> None:
        registry = load()
        kitchen = await registry.authenticate("kitchen-01", "kitchen-credential")
        phone = await registry.authenticate("phone-ada", "ada-credential-1")
        assert kitchen is not None
        assert phone is not None

        shared = principal_of(kitchen, user_roles=frozenset({Role.MEMBER}))
        personal = principal_of(phone, user_roles=frozenset({Role.MEMBER}))

        assert (shared.acts_as.kind, shared.acts_as.id, shared.roles) == (
            SubjectKind.TENANT,
            TENANT,
            frozenset(),
        )
        assert (personal.acts_as.kind, personal.acts_as.id) == (SubjectKind.USER, USER)
        assert personal.roles == frozenset({Role.MEMBER})
        assert (shared.device_id, personal.device_id) == (kitchen.id, phone.id)

    async def test_what_a_device_may_do_starts_as_its_kind_of_owner_says(self) -> None:
        registry = load()
        kitchen = await registry.authenticate("kitchen-01", "kitchen-credential")
        phone = await registry.authenticate("phone-ada", "ada-credential-1")
        assert kitchen is not None
        assert phone is not None

        assert (kitchen.owner.kind, kitchen.policy.max_effect) == (
            OwnerKind.TENANT,
            Effect.REVERSIBLE,
        )
        assert (phone.owner.kind, phone.policy.max_effect) == (OwnerKind.USER, Effect.IRREVERSIBLE)


class TestTheIdOfADevice:
    def test_the_same_device_has_the_same_id_every_time_it_is_read(self) -> None:
        assert device_id_for(TENANT, "kitchen-01") == device_id_for(TENANT, "kitchen-01")

    def test_another_device_or_another_tenant_has_another(self) -> None:
        assert device_id_for(TENANT, "kitchen-01") != device_id_for(TENANT, "kitchen-02")
        assert device_id_for(TENANT, "kitchen-01") != device_id_for(make_tenant_id(), "kitchen-01")

    async def test_a_conversation_carries_on_across_starts_of_the_edge(self) -> None:
        one = await load().authenticate("kitchen-01", "kitchen-credential")
        again = await load().authenticate("kitchen-01", "kitchen-credential")

        assert one is not None
        assert again is not None
        assert one.id == again.id

    async def test_an_id_that_is_written_down_is_the_one_used(self) -> None:
        identity = uuid4()
        registry = load(entry(id=identity))

        found = await registry.authenticate("kitchen-01", "kitchen-credential")

        assert found is not None
        assert found.id == identity


class TestWhatIsNotAFileOfDevices:
    @pytest.mark.parametrize(
        ("text", "message"),
        [
            ("", "expected a mapping"),
            ("- a list", "expected a mapping"),
            ("devices: nope", "'devices' list"),
            ("devices: []\nextra: 1", "unknown top-level keys"),
            ("devices: [1]", "device #1: expected a mapping"),
            (entry(colour="red"), "unknown keys"),
            (entry(token="short"), "at least 8 characters"),
            ("devices:\n  - {name: A, hardware_id: a-1}", "at least 8 characters"),
            (entry(owner="guest"), "'owner' must be"),
            ("devices:\n  - {name: A, token: a-credential}", "'hardware_id' is required"),
            (entry(capabilities="[flying]"), "device #1"),
            (entry(hardware_id="has space"), "hardware_id"),
            (entry(name="''"), "name"),
            (entry(display="{width: 100, height: 100}"), "display"),
        ],
    )
    def test_it_is_refused_and_says_where(self, text: str, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            load(text)

    @pytest.mark.parametrize("same", ["hardware_id", "token", "id"])
    def test_two_devices_cannot_be_one(self, same: str) -> None:
        identity = uuid4()
        first = {
            "name": "A",
            "hardware_id": "a-1",
            "token": "a-credential",
            "capabilities": "[audio_in]",
            "id": identity,
        }
        second = {
            "name": "B",
            "hardware_id": "b-1",
            "token": "b-credential",
            "capabilities": "[audio_in]",
            "id": uuid4(),
        }
        second[same] = first[same]
        text = "devices:\n" + "".join(
            "  -\n" + "".join(f"    {k}: {v}\n" for k, v in device.items())
            for device in (first, second)
        )

        with pytest.raises(ValueError, match="two devices have the same"):
            load(text)

    def test_what_is_wrong_is_said_and_what_was_written_is_not(self) -> None:
        text = entry(token="a-perfectly-good-secret", capabilities="[flying]")

        with pytest.raises(ValueError, match="device #1") as refused:
            load(text)

        assert "a-perfectly-good-secret" not in str(refused.value)


class TestWhereItIsAllowed:
    @pytest.mark.parametrize("environment", [Environment.PRODUCTION])
    def test_it_does_not_exist_in_production(self, environment: Environment) -> None:
        production = ServiceSettings(_env_file=None, environment=environment)

        with pytest.raises(AuthError, match="registered in the hub"):
            load(settings=production)
        with pytest.raises(AuthError):
            FileDeviceRegistry(production, [])

    @pytest.mark.parametrize("environment", [Environment.DEVELOPMENT, Environment.TEST])
    def test_it_does_in_development_and_in_tests(self, environment: Environment) -> None:
        assert load(settings=ServiceSettings(_env_file=None, environment=environment)) is not None

    async def test_a_file_is_read_from_its_path(self, tmp_path: Path) -> None:
        path = tmp_path / "devices.dev.yaml"
        path.write_text(DEVICES, encoding="utf-8")

        registry = load_devices_file(path, TEST, tenant_id=TENANT, user_id=USER)

        assert await registry.authenticate("kitchen-01", "kitchen-credential") is not None

    def test_a_file_that_is_not_there_is_an_error_that_names_it(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match=r"nowhere\.yaml"):
            load_devices_file(tmp_path / "nowhere.yaml", TEST, tenant_id=TENANT, user_id=USER)


class TestTheExampleFile:
    """``config/devices.example.yaml`` is what the documentation says to copy: it must work."""

    async def test_it_loads_and_each_device_in_it_is_known_by_the_credential_written_beside_it(
        self,
    ) -> None:
        registry = load_devices_file(EXAMPLE, TEST, tenant_id=TENANT, user_id=USER)

        known = {
            hardware_id: await registry.authenticate(hardware_id, token)
            for hardware_id, token in {
                "kitchen-01": "kitchen-dev-token",
                "living-room-01": "living-room-dev-token",
                "hallway-01": "hallway-dev-token",
                "study-01": "study-dev-token",
                "phone-01": "phone-dev-token",
            }.items()
        }

        assert all(record is not None for record in known.values())
        assert {
            hardware_id: record.owner.kind for hardware_id, record in known.items() if record
        } == {
            "kitchen-01": OwnerKind.TENANT,
            "living-room-01": OwnerKind.TENANT,
            "hallway-01": OwnerKind.TENANT,
            "study-01": OwnerKind.TENANT,
            "phone-01": OwnerKind.USER,
        }

    async def test_a_device_that_cannot_start_a_conversation_is_registered_without_a_microphone(
        self,
    ) -> None:
        registry = load_devices_file(EXAMPLE, TEST, tenant_id=TENANT, user_id=USER)

        hallway = await registry.authenticate("hallway-01", "hallway-dev-token")
        study = await registry.authenticate("study-01", "study-dev-token")

        assert hallway is not None
        assert study is not None
        assert Capability.AUDIO_IN not in hallway.capabilities | study.capabilities


HUB_TENANT: TenantId = make_tenant_id()


class Hub:
    """A hub that records what it is asked and answers with the devices it is told to."""

    def __init__(self) -> None:
        self.sent: list[httpx.Request] = []
        self.devices = DeviceList()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        return httpx.Response(200, content=self.devices.model_dump_json())

    def registry(self, tenant_id: TenantId = HUB_TENANT) -> HubDeviceRegistry:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self), base_url="http://hub.test")
        return HubDeviceRegistry(client, DevPrincipalDelegation(TEST), tenant_id)


def paired(token: str, **changes: object) -> SystemDeviceWithCredential:
    device = make_system_device(tenant_id=HUB_TENANT, **changes)  # type: ignore[arg-type]
    return SystemDeviceWithCredential(device=device, credential_hash=hash_credential(token))


class TestTheHubRegistry:
    async def test_it_knows_nobody_before_it_is_loaded(self) -> None:
        registry = Hub().registry()

        assert await registry.authenticate("kitchen-01", "whatever") is None

    async def test_reload_asks_the_hub_for_the_tenants_devices_as_the_tenant(self) -> None:
        hub = Hub()
        registry = hub.registry()

        await registry.reload()

        [request] = hub.sent
        assert request.url.path == "/v1/devices"
        assert request.headers["x-factotum-dev-subject"] == "tenant"

    async def test_a_device_is_known_by_its_hardware_id_and_its_credential_once_loaded(
        self,
    ) -> None:
        token = new_credential()
        hub = Hub()
        hub.devices = DeviceList(system=(paired(token, hardware_id="kitchen-01"),))
        registry = hub.registry()
        await registry.reload()

        found = await registry.authenticate("kitchen-01", token)

        assert found is not None
        assert found.hardware_id == "kitchen-01"

    @pytest.mark.parametrize(
        ("hardware_id", "wrong_token"),
        [
            ("kitchen-01", "not-the-credential"),
            ("hallway-01", "kitchen-credential"),
            ("nobody-01", "kitchen-credential"),
            ("kitchen-01", ""),
        ],
    )
    async def test_anything_else_is_nobody(self, hardware_id: str, wrong_token: str) -> None:
        hub = Hub()
        hub.devices = DeviceList(system=(paired("kitchen-credential", hardware_id="kitchen-01"),))
        registry = hub.registry()
        await registry.reload()

        assert await registry.authenticate(hardware_id, wrong_token) is None

    async def test_a_device_is_found_by_its_id_in_its_own_tenant_only(self) -> None:
        hub = Hub()
        hub.devices = DeviceList(system=(paired("kitchen-credential", hardware_id="kitchen-01"),))
        registry = hub.registry()
        await registry.reload()
        kitchen = await registry.authenticate("kitchen-01", "kitchen-credential")
        assert kitchen is not None

        assert await registry.get(HUB_TENANT, kitchen.id) == kitchen
        assert await registry.get(make_tenant_id(), kitchen.id) is None
        assert await registry.get(HUB_TENANT, make_device_id()) is None

    async def test_reloading_again_replaces_what_was_there_before(self) -> None:
        hub = Hub()
        hub.devices = DeviceList(system=(paired("first-credential", hardware_id="kitchen-01"),))
        registry = hub.registry()
        await registry.reload()

        hub.devices = DeviceList(system=(paired("second-credential", hardware_id="hallway-01"),))
        await registry.reload()

        assert await registry.authenticate("kitchen-01", "first-credential") is None
        assert await registry.authenticate("hallway-01", "second-credential") is not None

    async def test_a_device_acts_for_its_kind_of_owner_once_fetched_from_the_hub(self) -> None:
        hub = Hub()
        hub.devices = DeviceList(
            system=(
                paired(
                    "shared-credential",
                    hardware_id="kitchen-01",
                    owner=Owner.of_subject(Subject.tenant(HUB_TENANT)),
                ),
            )
        )
        registry = hub.registry()
        await registry.reload()

        kitchen = await registry.authenticate("kitchen-01", "shared-credential")

        assert kitchen is not None
        assert kitchen.owner.kind is OwnerKind.TENANT

    async def test_a_hub_that_answers_with_an_error_is_not_swallowed(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(refuse), base_url="http://hub.test"
        )
        registry = HubDeviceRegistry(client, DevPrincipalDelegation(TEST), HUB_TENANT)

        with pytest.raises(httpx.HTTPStatusError):
            await registry.reload()
