"""What a client is plugged into: who a device is, and the hands that run the commands it is
sent."""

from factotum.client_core import CommandRouter, DeviceIdentity
from factotum.client_core.testing import RecordingCommands, a_device
from factotum.contracts.capabilities import Capability
from factotum.contracts.cards import Card
from factotum.contracts.commands import (
    CommandResult,
    CommandStatus,
    LedPattern,
    SetLed,
    SetVolume,
    ShowCard,
)


class TestWhoADeviceIs:
    def test_the_credential_is_never_in_what_is_printed(self) -> None:
        identity = a_device(token="a-very-secret-credential")

        assert "a-very-secret-credential" not in repr(identity)
        assert "a-very-secret-credential" not in str(identity)

    def test_an_identity_is_what_it_is_made_of(self) -> None:
        identity = DeviceIdentity(
            hardware_id="kitchen-01",
            token="t",
            capabilities=frozenset({Capability.AUDIO_OUT}),
        )

        assert identity.display is None
        assert identity.capabilities == {Capability.AUDIO_OUT}


class TestTheHandsOfADevicePutTogether:
    def hands(self) -> tuple[CommandRouter, RecordingCommands, RecordingCommands]:
        volume = RecordingCommands(capabilities=frozenset({Capability.AUDIO_OUT}))
        light = RecordingCommands(capabilities=frozenset({Capability.LED}))
        return CommandRouter(volume, light), volume, light

    async def test_each_command_goes_to_the_hands_that_have_what_it_needs(self) -> None:
        router, volume, light = self.hands()

        first = await router.execute(SetVolume(level=30))
        second = await router.execute(SetLed(pattern=LedPattern.BLINK))

        assert (first.status, second.status) == (CommandStatus.OK, CommandStatus.OK)
        assert volume.commands == [SetVolume(level=30)]
        assert light.commands == [SetLed(pattern=LedPattern.BLINK)]

    async def test_what_the_hands_answer_is_what_the_edge_is_told(self) -> None:
        failed = CommandResult(status=CommandStatus.FAILED, detail="the amplifier is off")
        volume = RecordingCommands(failed, capabilities=frozenset({Capability.AUDIO_OUT}))

        result = await CommandRouter(volume).execute(SetVolume(level=30))

        assert result == failed

    async def test_a_command_that_no_hands_have_what_it_needs_for_is_unsupported(self) -> None:
        router, volume, light = self.hands()

        result = await router.execute(ShowCard(card=Card(title="Recipe")))

        assert result.status is CommandStatus.UNSUPPORTED
        assert result.detail == "this device has no 'display'"
        assert volume.commands == light.commands == []

    async def test_a_device_with_no_hands_does_nothing_and_says_so(self) -> None:
        result = await CommandRouter().execute(SetVolume(level=30))

        assert result.status is CommandStatus.UNSUPPORTED

    def test_what_the_device_can_do_is_what_all_of_its_hands_can(self) -> None:
        router, _, _ = self.hands()

        assert router.capabilities == {Capability.AUDIO_OUT, Capability.LED}
        assert CommandRouter().capabilities == frozenset()

    async def test_when_two_hands_could_the_first_does_it(self) -> None:
        first = RecordingCommands(capabilities=frozenset({Capability.AUDIO_OUT}))
        second = RecordingCommands(capabilities=frozenset({Capability.AUDIO_OUT}))

        await CommandRouter(first, second).execute(SetVolume(level=30))

        assert (len(first.commands), len(second.commands)) == (1, 0)
