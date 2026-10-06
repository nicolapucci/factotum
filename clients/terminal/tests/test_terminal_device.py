import pytest
from pydantic import AnyHttpUrl

from factotum.client_core.testing import RecordingRenderer
from factotum.contracts.audio import wav_of
from factotum.contracts.capabilities import Capability
from factotum.contracts.cards import Card
from factotum.contracts.commands import (
    ClearDisplay,
    CommandStatus,
    DeviceCommand,
    LedPattern,
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
from factotum.terminal.device import TerminalDevice

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
