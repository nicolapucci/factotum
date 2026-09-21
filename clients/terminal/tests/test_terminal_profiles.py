from pathlib import Path
from typing import Any

import pytest
import yaml

from factotum.contracts.capabilities import Capability
from factotum.contracts.edge import PROTOCOL_VERSION, Hello
from factotum.terminal.profiles import DEFAULT_PROFILE, PROFILES, get_profile

VOICE = frozenset({Capability.AUDIO_IN, Capability.AUDIO_OUT})
EXAMPLE = Path(__file__).resolve().parents[3] / "config" / "devices.example.yaml"


def example_devices() -> dict[str, dict[str, Any]]:
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    return {
        device["hardware_id"]: device
        for device in document["devices"]
        if device.get("kind", "system") == "system"
    }


class TestTheProfiles:
    def test_there_is_one_for_each_kind_of_device_the_edge_has_to_tell_apart(self) -> None:
        def caps(name: str) -> frozenset[Capability]:
            return get_profile(name).capabilities

        voice_only = [p for p in PROFILES.values() if p.capabilities >= VOICE and p.display is None]
        screen_and_voice = [
            p for p in PROFILES.values() if p.capabilities >= VOICE | {Capability.DISPLAY}
        ]
        screen_alone = [p for p in PROFILES.values() if p.capabilities == {Capability.DISPLAY}]
        no_microphone = [
            p
            for p in PROFILES.values()
            if Capability.AUDIO_IN not in p.capabilities and Capability.AUDIO_OUT in p.capabilities
        ]

        assert voice_only, "a speaker"
        assert screen_and_voice, "a screen with a voice"
        assert screen_alone, "a screen with no voice"
        assert no_microphone, "something that plays and cannot be spoken to"
        assert caps("screen") == {Capability.DISPLAY}

    def test_the_default_is_one_of_them(self) -> None:
        assert DEFAULT_PROFILE in PROFILES

    @pytest.mark.parametrize("name", sorted(PROFILES))
    def test_each_is_a_device_the_edge_would_accept_the_hello_of(self, name: str) -> None:
        profile = get_profile(name)

        hello = Hello(
            protocol_version=PROTOCOL_VERSION,
            hardware_id=profile.hardware_id,
            token=profile.token,
            capabilities=profile.capabilities,
            display=profile.display,
        )

        assert hello.capabilities == profile.capabilities

    @pytest.mark.parametrize("name", sorted(PROFILES))
    def test_a_screen_is_described_where_there_is_one_and_only_there(self, name: str) -> None:
        profile = get_profile(name)

        assert (profile.display is not None) == (Capability.DISPLAY in profile.capabilities)

    def test_no_two_are_the_same_device(self) -> None:
        profiles = list(PROFILES.values())

        assert len({p.hardware_id for p in profiles}) == len(profiles)
        assert len({p.token for p in profiles}) == len(profiles)

    def test_a_profile_that_does_not_exist_says_which_do(self) -> None:
        with pytest.raises(ValueError, match=r"the profiles are .*speaker"):
            get_profile("toaster")


class TestTheExampleFileOfDevices:
    """A profile is a device of ``config/devices.example.yaml``: the same hardware id, credential,
    capabilities and screen, so that ``factotum-terminal --profile speaker`` is accepted by an edge
    that was given a copy of it."""

    @pytest.mark.parametrize("name", sorted(PROFILES))
    def test_the_device_of_each_profile_is_in_it_as_the_profile_says(self, name: str) -> None:
        profile = get_profile(name)
        device = example_devices()[profile.hardware_id]

        assert device["token"] == profile.token
        assert {Capability(c) for c in device["capabilities"]} == set(profile.capabilities)
        if profile.display is None:
            assert "display" not in device
        else:
            assert device["display"] == profile.display.model_dump(exclude_defaults=True)

    def test_every_device_of_it_has_a_profile(self) -> None:
        assert set(example_devices()) == {p.hardware_id for p in PROFILES.values()}

    def test_there_is_a_personal_device_among_them_and_shared_ones(self) -> None:
        owners = {device["owner"] for device in example_devices().values()}

        assert owners == {"tenant", "user"}
