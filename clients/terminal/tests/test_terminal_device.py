import pytest
from pydantic import AnyHttpUrl

from factotum.client_core.testing import RecordingRenderer
from factotum.contracts.audio import wav_of
from factotum.contracts.capabilities import Capability
from factotum.contracts.cards import Card
from factotum.contracts.commands import (
    AdjustVolume,
    ClearDisplay,
    CommandStatus,
    DeviceCommand,
    GetState,
    LedPattern,
    Pause,
    PlayAudio,
    PlayUrl,
    Resume,
    SetLed,
    SetMute,
    SetVolume,
    ShowCard,
    Speak,
    Stop,
    VolumeDirection,
)
from factotum.terminal.device import VOLUME_STEP, TerminalDevice

EVERYTHING = frozenset(Capability)
RADIO = AnyHttpUrl("https://radio.example.org/stream")


def device(
    capabilities: frozenset[Capability] = EVERYTHING,
) -> tuple[TerminalDevice, RecordingRenderer]:
    renderer = RecordingRenderer()
    return TerminalDevice(capabilities, renderer), renderer


class TestWhatTheHardwareDoes:
    async def test_it_speaks(self) -> None:
        hardware, shown = device()

        result = await hardware.execute(Speak(text="Dinner is ready"))

        assert result.succeeded
        assert shown.of("speech") == ["Dinner is ready"]
        assert hardware.state.spoken == ["Dinner is ready"]

    async def test_it_sets_the_volume(self) -> None:
        hardware, shown = device()

        result = await hardware.execute(SetVolume(level=30))

        assert result.succeeded
        assert hardware.state.volume == 30
        assert shown.of("event") == ["volume 30"]

    async def test_it_turns_the_volume_up_and_down_by_what_was_said(self) -> None:
        hardware, shown = device()  # the volume starts at 50

        up = await hardware.execute(AdjustVolume(direction=VolumeDirection.UP, amount=10))
        down = await hardware.execute(AdjustVolume(direction=VolumeDirection.DOWN, amount=30))

        assert up.succeeded
        assert down.succeeded
        assert hardware.state.volume == 30
        assert shown.of("event") == ["volume 60", "volume 30"]

    async def test_and_by_its_own_step_when_nobody_said(self) -> None:
        hardware, shown = device()

        await hardware.execute(AdjustVolume(direction=VolumeDirection.UP))
        await hardware.execute(AdjustVolume(direction=VolumeDirection.DOWN))
        await hardware.execute(AdjustVolume(direction=VolumeDirection.DOWN))

        assert VOLUME_STEP == 10
        assert hardware.state.volume == 40
        assert shown.of("event") == ["volume 60", "volume 50", "volume 40"]

    async def test_a_device_made_with_another_step_turns_it_by_that(self) -> None:
        hardware = TerminalDevice(EVERYTHING, RecordingRenderer(), volume_step=3)

        await hardware.execute(AdjustVolume(direction=VolumeDirection.UP))
        await hardware.execute(AdjustVolume(direction=VolumeDirection.DOWN, amount=20))

        assert hardware.state.volume == 33

    async def test_a_change_of_the_volume_stops_at_either_end(self) -> None:
        hardware, shown = device()

        await hardware.execute(AdjustVolume(direction=VolumeDirection.UP, amount=100))
        await hardware.execute(AdjustVolume(direction=VolumeDirection.DOWN, amount=100))
        await hardware.execute(AdjustVolume(direction=VolumeDirection.DOWN))

        assert hardware.state.volume == 0
        assert shown.of("event") == ["volume 100", "volume 0", "volume 0"]

    async def test_it_says_the_level_the_volume_came_to(self) -> None:
        hardware, _ = device()

        set_to = await hardware.execute(SetVolume(level=30))
        turned = await hardware.execute(AdjustVolume(direction=VolumeDirection.UP))

        assert (set_to.data, turned.data) == ({"volume": 30}, {"volume": 40})

    async def test_it_mutes_the_sound_and_keeps_the_level(self) -> None:
        hardware, shown = device()

        muted = await hardware.execute(SetMute(muted=True))
        assert (hardware.state.muted, hardware.state.volume, muted.data) == (
            True,
            50,
            {"muted": True},
        )
        await hardware.execute(SetMute(muted=False))

        assert (hardware.state.muted, hardware.state.volume) == (False, 50)
        assert shown.of("event") == ["muted", "unmuted"]
        assert "muted" not in hardware.state.describe()

    async def test_setting_or_turning_the_volume_brings_the_sound_back(self) -> None:
        hardware, _ = device()
        await hardware.execute(SetMute(muted=True))
        assert "muted" in hardware.state.describe()

        await hardware.execute(SetVolume(level=20))
        assert hardware.state.muted is False
        await hardware.execute(SetMute(muted=True))
        await hardware.execute(AdjustVolume(direction=VolumeDirection.DOWN))

        assert (hardware.state.muted, hardware.state.volume) == (False, 10)

    async def test_it_says_how_it_is_and_changes_nothing(self) -> None:
        hardware, shown = device()
        await hardware.execute(SetVolume(level=35))
        await hardware.execute(SetMute(muted=True))
        before, events = hardware.state.describe(), list(shown.of("event"))

        result = await hardware.execute(GetState())

        assert result.data == {"volume": 35, "muted": True}
        assert hardware.state.describe() == before
        assert shown.of("event") == events

    async def test_it_lights_a_light(self) -> None:
        hardware, shown = device()

        await hardware.execute(SetLed(pattern=LedPattern.BLINK, color="#00ff00"))

        assert (hardware.state.led_pattern, hardware.state.led_color) == (
            LedPattern.BLINK,
            "#00ff00",
        )
        assert shown.of("event") == ["light blink #00ff00"]

    async def test_it_shows_a_card_and_clears_the_screen(self) -> None:
        hardware, shown = device()
        card = Card(title="Recipe")

        await hardware.execute(ShowCard(card=card))
        assert hardware.state.card == card
        await hardware.execute(ClearDisplay())

        assert hardware.state.card is None
        assert shown.of("card") == [card]
        assert shown.of("event") == ["screen cleared"]

    async def test_it_plays_pauses_resumes_and_stops_the_music(self) -> None:
        hardware, shown = device()

        await hardware.execute(PlayUrl(url=RADIO, title="Radio"))
        assert (hardware.state.media, hardware.state.media_title) == ("playing", "Radio")
        await hardware.execute(Pause())
        assert hardware.state.media == "paused"
        await hardware.execute(Resume())
        assert hardware.state.media == "playing"
        await hardware.execute(Stop())

        assert (hardware.state.media, hardware.state.media_title) == ("stopped", None)
        assert shown.of("media") == [("Radio", str(RADIO))]
        assert shown.of("event") == ["music paused", "music resumed", "music stopped"]

    async def test_music_with_no_title_is_called_by_its_address(self) -> None:
        hardware, _ = device()

        await hardware.execute(PlayUrl(url=RADIO))

        assert hardware.state.media_title == str(RADIO)


class TestWhenItCannotDoIt:
    async def test_it_cannot_pause_what_is_not_playing_nor_resume_what_is_not_paused(self) -> None:
        hardware, shown = device()

        paused = await hardware.execute(Pause())
        resumed = await hardware.execute(Resume())

        assert (paused.status, resumed.status) == (CommandStatus.FAILED, CommandStatus.FAILED)
        assert paused.detail == "nothing is playing"
        assert resumed.detail == "nothing is paused"
        assert shown.of("event") == []

    @pytest.mark.parametrize(
        ("command", "missing"),
        [
            (Speak(text="hello"), "audio_out"),
            (SetVolume(level=10), "audio_out"),
            (AdjustVolume(direction=VolumeDirection.UP), "audio_out"),
            (SetMute(muted=True), "audio_out"),
            (GetState(), "audio_out"),
            (SetLed(pattern=LedPattern.SOLID), "led"),
            (ShowCard(card=Card(title="x")), "display"),
            (PlayUrl(url=RADIO), "media_player"),
            (Pause(), "media_player"),
        ],
    )
    async def test_what_the_hardware_does_not_have_is_unsupported_and_nothing_happens(
        self, command: DeviceCommand, missing: str
    ) -> None:
        hardware, shown = device(frozenset({Capability.AUDIO_IN}))
        before = hardware.state.describe()

        result = await hardware.execute(command)

        assert result.status is CommandStatus.UNSUPPORTED
        assert missing in (result.detail or "")
        assert shown.shown == []
        assert hardware.state.describe() == before

    async def test_a_clip_is_not_for_these_hands_and_is_not_said_to_have_been_played(self) -> None:
        hardware, shown = device()
        clip = PlayAudio(text="Hi.", audio=wav_of(bytes(100), 22_050))

        result = await hardware.execute(clip)

        assert result.status is CommandStatus.UNSUPPORTED
        assert "play_audio" in (result.detail or "")
        assert shown.shown == []
        assert hardware.state.spoken == []


class TestWhatItIsDoing:
    async def test_it_can_say_what_the_hardware_is_doing(self) -> None:
        hardware, _ = device()
        assert hardware.state.describe() == ("volume 50; light off; music stopped; screen: nothing")

        await hardware.execute(SetVolume(level=20))
        await hardware.execute(SetLed(pattern=LedPattern.PULSE, color="#0000ff"))
        await hardware.execute(PlayUrl(url=RADIO, title="Radio"))
        await hardware.execute(ShowCard(card=Card(title="Recipe")))

        assert hardware.state.describe() == (
            "volume 20; light pulse #0000ff; music playing (Radio); screen: a card: Recipe"
        )
