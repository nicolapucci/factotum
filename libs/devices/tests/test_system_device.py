import inspect
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from pydantic import AnyHttpUrl, TypeAdapter, ValidationError

from factotum.contracts.audio import wav_of
from factotum.contracts.capabilities import Capability
from factotum.contracts.cards import Card
from factotum.contracts.commands import (
    COMMAND_MODELS,
    ClearDisplay,
    CommandResult,
    CommandStatus,
    DeviceCommand,
    LedPattern,
    MediaKind,
    Pause,
    PlayAudio,
    PlayUrl,
    Resume,
    SetLed,
    SetVolume,
    ShowCard,
    Speak,
    Stop,
)
from factotum.contracts.errors import CapabilityNotSupportedError
from factotum.devices.system_device import SystemDevice
from factotum.devices.testing import FakeSystemDevice

command_adapter: TypeAdapter[Any] = TypeAdapter(DeviceCommand)

ALL_CAPABILITIES = frozenset(Capability)
CARD = Card(title="Pasta al pomodoro")
CLIP = wav_of(bytes(range(100)) * 2, 22_050)
RADIO = "https://radio.example.org/stream"

Call = Callable[[SystemDevice], Awaitable[CommandResult]]

CALLS: list[tuple[str, Call, DeviceCommand]] = [
    ("speak", lambda d: d.speak("Ciao", language="it"), Speak(text="Ciao", language="it")),
    (
        "play_audio",
        lambda d: d.play_audio("Ciao", CLIP),
        PlayAudio(text="Ciao", audio=CLIP),
    ),
    (
        "play_url",
        lambda d: d.play_url(RADIO, kind=MediaKind.VIDEO, title="Radio"),
        PlayUrl(url=AnyHttpUrl(RADIO), kind=MediaKind.VIDEO, title="Radio"),
    ),
    ("pause", lambda d: d.pause(), Pause()),
    ("resume", lambda d: d.resume(), Resume()),
    ("stop", lambda d: d.stop(), Stop()),
    ("set_volume", lambda d: d.set_volume(30), SetVolume(level=30)),
    ("show_card", lambda d: d.show_card(CARD), ShowCard(card=CARD)),
    ("clear_display", lambda d: d.clear_display(), ClearDisplay()),
    (
        "set_led",
        lambda d: d.set_led(LedPattern.PULSE, "#00ff00"),
        SetLed(pattern=LedPattern.PULSE, color="#00ff00"),
    ),
]


def command_names() -> set[str]:
    return {str(model.model_fields["name"].default) for model in COMMAND_MODELS}


class TestTheInterfaceAndTheVocabularyDoNotDiverge:
    def test_every_command_has_a_method_and_every_method_has_a_command(self) -> None:
        methods = {
            name
            for name, member in inspect.getmembers(SystemDevice, inspect.iscoroutinefunction)
            if not name.startswith("_") and name not in {"send", "execute"}
        }

        assert methods == command_names()

    def test_the_calls_under_test_cover_the_whole_vocabulary(self) -> None:
        assert {name for name, _, _ in CALLS} == command_names()

    @pytest.mark.parametrize(("name", "call", "expected"), CALLS, ids=[c[0] for c in CALLS])
    async def test_each_method_sends_exactly_its_command(
        self, name: str, call: Call, expected: DeviceCommand
    ) -> None:
        device = FakeSystemDevice(ALL_CAPABILITIES)

        result = await call(device)

        assert result.succeeded
        assert device.sent == [expected]
        assert expected.name == name


class TestCapabilities:
    @pytest.mark.parametrize(("name", "call", "expected"), CALLS, ids=[c[0] for c in CALLS])
    async def test_a_device_without_the_capability_is_never_sent_the_command(
        self, name: str, call: Call, expected: DeviceCommand
    ) -> None:
        device = FakeSystemDevice(ALL_CAPABILITIES - {expected.requires})

        with pytest.raises(CapabilityNotSupportedError) as caught:
            await call(device)

        assert device.sent == []
        assert caught.value.details == {"capability": expected.requires.value, "command": name}

    async def test_a_speaker_without_a_screen_can_speak_but_not_show(self) -> None:
        speaker = FakeSystemDevice({Capability.AUDIO_OUT})

        await speaker.speak("Ciao")
        with pytest.raises(CapabilityNotSupportedError):
            await speaker.show_card(CARD)

        assert [command.name for command in speaker.sent] == ["speak"]

    async def test_a_screen_without_a_microphone_can_still_be_shown_things(self) -> None:
        screen = FakeSystemDevice({Capability.DISPLAY})

        assert (await screen.show_card(CARD)).succeeded
        assert not screen.supports(Capability.AUDIO_IN)

    def test_supports_reflects_the_declared_capabilities(self) -> None:
        device = FakeSystemDevice({Capability.AUDIO_OUT, Capability.LED})

        assert device.supports(Capability.LED)
        assert not device.supports(Capability.DISPLAY)
        assert device.capabilities == {Capability.AUDIO_OUT, Capability.LED}

    def test_the_capabilities_cannot_be_changed_afterwards(self) -> None:
        declared = {Capability.AUDIO_OUT}
        device = FakeSystemDevice(declared)

        declared.add(Capability.DISPLAY)

        assert Capability.DISPLAY not in device.capabilities
        assert isinstance(device.capabilities, frozenset)


class TestExecute:
    async def test_a_command_received_as_data_runs_after_the_same_check(self) -> None:
        command = command_adapter.validate_python({"name": "set_volume", "level": 15})
        device = FakeSystemDevice({Capability.AUDIO_OUT})

        result = await device.execute(command)

        assert result.succeeded
        assert device.sent == [SetVolume(level=15)]

    async def test_it_refuses_a_command_the_device_cannot_run(self) -> None:
        device = FakeSystemDevice({Capability.AUDIO_OUT})

        with pytest.raises(CapabilityNotSupportedError, match="'media_player'"):
            await device.execute(Pause())


class TestResults:
    async def test_the_result_comes_from_whoever_implements_send(self) -> None:
        failure = CommandResult(status=CommandStatus.TIMEOUT, detail="no ack in 5s")
        device = FakeSystemDevice(ALL_CAPABILITIES, result=failure)

        assert await device.speak("Ciao") == failure

    async def test_an_offline_device_reports_it(self) -> None:
        device = FakeSystemDevice(ALL_CAPABILITIES)
        device.offline = True

        result = await device.speak("Ciao")

        assert result.status is CommandStatus.OFFLINE
        assert device.sent == []


class TestInvalidArguments:
    @pytest.mark.parametrize("level", [-1, 101])
    async def test_an_out_of_range_volume_is_rejected_before_anything_is_sent(
        self, level: int
    ) -> None:
        device = FakeSystemDevice(ALL_CAPABILITIES)

        with pytest.raises(ValidationError):
            await device.set_volume(level)

        assert device.sent == []

    async def test_empty_speech_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            await FakeSystemDevice(ALL_CAPABILITIES).speak("")

    async def test_a_media_url_must_be_a_valid_http_url(self) -> None:
        with pytest.raises(ValidationError):
            await FakeSystemDevice(ALL_CAPABILITIES).play_url("not a url")


def test_the_interface_cannot_be_instantiated_without_a_way_to_send() -> None:
    with pytest.raises(TypeError, match="abstract"):
        SystemDevice(FakeSystemDevice().device_id, ALL_CAPABILITIES)  # type: ignore[abstract]
