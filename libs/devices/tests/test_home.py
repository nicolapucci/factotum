from pathlib import Path

import pytest

from factotum.contracts.effects import Effect
from factotum.contracts.identity import OwnerKind
from factotum.contracts.testing import make_tenant_id, make_user_id
from factotum.devices.home import (
    MIN_TOKEN_CHARS,
    Home,
    device_id_for,
    external_device_id_for,
    parse_home,
)

EXAMPLE = Path(__file__).resolve().parents[3] / "config" / "devices.example.yaml"

TENANT = make_tenant_id()
USER = make_user_id()

SPEAKER = """
  - name: Kitchen speaker
    hardware_id: kitchen-01
    token: kitchen-credential
    owner: tenant
    capabilities: [audio_in, audio_out]
"""
LAMP = """
  - kind: external
    name: Living room lamp
    owner: tenant
    aliases: [lamp, the lamp]
    tags: [light]
"""
SAME_ID = "5d9e1a54-08f3-4c0e-b5f0-000000000001"
GARAGE = """
  - kind: external
    name: Garage door
    sensitivity: irreversible
    external_refs: {entity_id: cover.garage}
"""


def home(devices: str = SPEAKER + LAMP + GARAGE, extra: str = "") -> Home:
    return parse_home(f"devices:{devices}\n{extra}", tenant_id=TENANT, user_id=USER, source="home")


def refused(text: str) -> str:
    with pytest.raises(ValueError, match=r"^home: ") as error:
        parse_home(text, tenant_id=TENANT, user_id=USER, source="home")
    return str(error.value)


class TestTheDevices:
    def test_the_system_devices_and_the_external_ones_are_told_apart(self) -> None:
        described = home()

        assert [record.name for record, _ in described.system] == ["Kitchen speaker"]
        assert [record.name for record in described.external] == ["Living room lamp", "Garage door"]

    def test_a_system_device_is_what_it_was_before_the_file_described_more(self) -> None:
        [(speaker, token)] = home(SPEAKER).system

        assert (speaker.hardware_id, token, speaker.owner.kind) == (
            "kitchen-01",
            "kitchen-credential",
            OwnerKind.TENANT,
        )
        assert speaker.id == device_id_for(TENANT, "kitchen-01")
        assert speaker.policy.max_effect is Effect.REVERSIBLE

    def test_an_external_device_is_known_by_its_name_and_has_no_credential(self) -> None:
        [lamp, garage] = home().external

        assert lamp.id == external_device_id_for(TENANT, "Living room lamp")
        assert (lamp.aliases, lamp.tags, lamp.sensitivity) == (
            ("lamp", "the lamp"),
            frozenset({"light"}),
            Effect.READ_ONLY,
        )
        assert garage.sensitivity is Effect.IRREVERSIBLE
        assert garage.external_refs == {"entity_id": "cover.garage"}

    def test_an_external_device_may_be_a_persons_own(self) -> None:
        [mine] = home("\n  - kind: external\n    name: My lamp\n    owner: user\n").external

        assert (mine.owner.kind, mine.owner.id) == (OwnerKind.USER, USER)
        assert mine.policy.max_effect is Effect.IRREVERSIBLE

    def test_what_a_shared_device_may_do_is_capped_unless_the_file_says_otherwise(self) -> None:
        [shared, allowed, careful] = home(
            "\n  - kind: external\n    name: A\n"
            "\n  - kind: external\n    name: B\n    policy: {max_effect: irreversible}\n"
            "\n  - kind: external\n    name: C\n    policy: {requires_approval: true}\n"
        ).external

        assert shared.policy.max_effect is Effect.REVERSIBLE
        assert allowed.policy.max_effect is Effect.IRREVERSIBLE
        assert careful.policy.requires_approval is True

    def test_a_system_device_can_have_a_policy_too(self) -> None:
        [(speaker, _)] = home(
            SPEAKER + "    policy: {max_effect: read_only, requires_approval: true}\n"
        ).system

        assert speaker.policy.max_effect is Effect.READ_ONLY
        assert speaker.policy.requires_approval is True

    def test_an_id_that_is_written_down_is_the_one_used(self) -> None:
        written = "5d9e1a54-08f3-4c0e-b5f0-000000000001"

        [lamp] = home(f"\n  - kind: external\n    name: Lamp\n    id: {written}\n").external

        assert str(lamp.id) == written

    def test_the_ids_of_the_two_kinds_never_meet(self) -> None:
        assert external_device_id_for(TENANT, "kitchen-01") != device_id_for(TENANT, "kitchen-01")
        assert external_device_id_for(TENANT, "Lamp") == external_device_id_for(TENANT, "Lamp")
        assert external_device_id_for(TENANT, "Lamp") != external_device_id_for(
            make_tenant_id(), "Lamp"
        )

    def test_a_file_with_no_devices_to_speak_of_is_still_a_home(self) -> None:
        assert home(" []") == Home()


class TestTheBindings:
    BOUND = """
bindings:
  - skill: lamp
    device: Living room lamp
    params: {entity_id: light.living_room, host: homeassistant.local, dimmable: true, port: 8123}
  - skill: lamp
    device: Garage door
    enabled: false
"""

    def test_a_skill_is_bound_to_a_device_by_the_name_of_each_with_the_devices_own_parameters(
        self,
    ) -> None:
        described = home(extra=self.BOUND)
        [lamp, garage] = described.bindings

        assert (lamp.skill, lamp.device_name, lamp.device_id) == (
            "lamp",
            "Living room lamp",
            described.external[0].id,
        )
        assert lamp.params == {
            "entity_id": "light.living_room",
            "host": "homeassistant.local",
            "dimmable": True,
            "port": 8123,
        }
        assert (garage.enabled, garage.params) == (False, {})

    def test_a_home_may_have_no_bindings_at_all(self) -> None:
        assert home().bindings == ()
        assert home(extra="bindings:\n").bindings == ()

    @pytest.mark.parametrize(
        ("binding", "message"),
        [
            ("  - skill: lamp\n    device: A stove\n", "binding #1: 'device' must be the name of"),
            ("  - device: Garage door\n", "binding #1: 'skill' is required"),
            (
                "  - skill: lamp\n    device: Garage door\n    volume: 3\n",
                "binding #1: unknown keys: ['volume']",
            ),
            ("  - just a string\n", "binding #1: expected a mapping"),
            (
                "  - skill: lamp\n    device: Garage door\n    params: {a: [1, 2]}\n",
                "binding #1: 'params' must map names to text, numbers or booleans",
            ),
            (
                "  - skill: lamp\n    device: Garage door\n    enabled: maybe\n",
                "binding #1: 'enabled' must be true or false",
            ),
            (
                "  - skill: lamp\n    device: Garage door\n"
                "  - skill: lamp\n    device: Garage door\n",
                "a skill is bound to the same device twice",
            ),
        ],
    )
    def test_a_binding_that_does_not_make_sense_is_refused_and_says_where(
        self, binding: str, message: str
    ) -> None:
        with pytest.raises(ValueError, match=message.replace("[", r"\[").replace("]", r"\]")):
            home(extra=f"bindings:\n{binding}")

    def test_bindings_are_a_list(self) -> None:
        with pytest.raises(ValueError, match="'bindings' must be a list"):
            home(extra="bindings: {skill: lamp}\n")

    def test_a_binding_can_only_name_an_external_device(self) -> None:
        with pytest.raises(ValueError, match="must be the name of an external device"):
            home(extra="bindings:\n  - skill: lamp\n    device: Kitchen speaker\n")


class TestWhatIsNotAHome:
    @pytest.mark.parametrize(
        ("text", "message"),
        [
            ("", "expected a mapping with a 'devices' list"),
            ("devices: 3", "expected a mapping with a 'devices' list"),
            ("devices: []\nrooms: []", "unknown top-level keys: ['rooms']"),
            ("devices:\n  - just a string", "device #1: expected a mapping"),
            ("devices:\n  - kind: robot\n    name: R2", "device #1: 'kind' must be"),
            ("devices:\n  - kind: external\n    owner: nobody\n    name: A", "'owner' must be"),
            (
                "devices:\n  - kind: external\n    name: A\n    color: red",
                "unknown keys: ['color']",
            ),
            ("devices:\n  - kind: external\n    owner: tenant", "'name' is required"),
            (
                "devices:\n  - kind: external\n    name: A\n    sensitivity: severe",
                "'sensitivity' must be one of",
            ),
            (
                "devices:\n  - kind: external\n    name: A\n    external_refs: {a: 1}",
                "'external_refs' must map names to strings",
            ),
            (
                "devices:\n  - kind: external\n    name: A\n    policy: 3",
                "'policy' must be a mapping",
            ),
            (
                "devices:\n  - kind: external\n    name: A\n    policy: {speed: 3}",
                "unknown policy keys: ['speed']",
            ),
            (
                "devices:\n  - kind: external\n    name: A\n    policy: {requires_approval: sure}",
                "'requires_approval' must be true or false",
            ),
            (
                "devices:\n  - kind: external\n    name: A\n    policy: {max_effect: enormous}",
                "'max_effect' must be one of",
            ),
            (
                "devices:\n  - name: S\n    hardware_id: s-1\n    token: short\n",
                f"'token' must be a string of at least {MIN_TOKEN_CHARS} characters",
            ),
            ("devices:\n  - name: S\n    token: long-enough-token\n", "'hardware_id' is required"),
            (
                "devices:\n  - name: S\n    hardware_id: s-1\n    token: long-enough-token\n"
                "    capabilities: [telepathy]\n",
                "device #1",
            ),
        ],
    )
    def test_it_is_refused_and_says_where(self, text: str, message: str) -> None:
        assert message in refused(text)

    @pytest.mark.parametrize(
        ("devices", "same"),
        [
            (SPEAKER + SPEAKER.replace("kitchen-credential", "other-credential"), "hardware id"),
            (SPEAKER + SPEAKER.replace("kitchen-01", "kitchen-02"), "token"),
            (LAMP + LAMP, "name"),
            (
                f"\n  - kind: external\n    name: A\n    id: {SAME_ID}\n"
                f"\n  - kind: external\n    name: B\n    id: {SAME_ID}\n",
                "id",
            ),
            (
                SPEAKER + f"    id: {SAME_ID}\n"
                f"\n  - kind: external\n    name: A\n    id: {SAME_ID}\n",
                "id",
            ),
        ],
    )
    def test_two_devices_cannot_be_one(self, devices: str, same: str) -> None:
        with pytest.raises(ValueError, match=f"two devices have the same {same}"):
            home(devices)

    def test_what_is_wrong_is_said_and_what_was_written_is_not(self) -> None:
        secret = "a-credential-in-the-wrong-place-that-must-not-leak"

        message = refused(
            f"devices:\n  - name: S\n    hardware_id: 'not valid!'\n    token: {secret}\n"
        )

        assert "device #1" in message
        assert secret not in message


class TestTheExampleFile:
    """``config/devices.example.yaml`` is what the documentation says to copy: it must work."""

    def example(self) -> Home:
        return parse_home(
            EXAMPLE.read_text(encoding="utf-8"),
            tenant_id=TENANT,
            user_id=USER,
            source=EXAMPLE.name,
        )

    def test_it_describes_the_devices_of_a_home_of_each_kind(self) -> None:
        described = self.example()

        assert len(described.system) == 5
        assert {record.name for record in described.external} == {
            "Living room lamp",
            "Garage door",
            "Bedroom lamp",
        }

    def test_a_garage_door_is_a_more_serious_thing_to_act_on_than_a_lamp(self) -> None:
        by_name = {record.name: record for record in self.example().external}

        assert by_name["Garage door"].sensitivity is Effect.IRREVERSIBLE
        assert by_name["Living room lamp"].sensitivity is Effect.READ_ONLY
        assert by_name["Bedroom lamp"].owner.kind is OwnerKind.USER

    def test_one_shared_device_is_allowed_to_start_what_cannot_be_undone_and_the_others_are_not(
        self,
    ) -> None:
        policies = {record.name: record.policy for record, _ in self.example().system}

        assert policies["Living room display"].max_effect is Effect.IRREVERSIBLE
        assert policies["Kitchen speaker"].max_effect is Effect.REVERSIBLE
        assert policies["My phone"].max_effect is Effect.IRREVERSIBLE

    def test_every_binding_is_of_a_skill_to_a_device_of_the_file(self) -> None:
        described = self.example()
        devices = {record.id for record in described.external}

        assert {b.skill for b in described.bindings} == {"lamp", "garage-door"}
        assert all(b.device_id in devices for b in described.bindings)
        assert all("entity_id" in b.params for b in described.bindings)
