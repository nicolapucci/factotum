"""What a device does when it is told to, and what it says when it cannot: the hands of a
speaker."""

from factotum.client_audio import Speaker
from factotum.client_audio.testing import FakeBackend
from factotum.client_core.testing import RecordingRenderer
from factotum.contracts.audio import wav_of
from factotum.contracts.capabilities import Capability
from factotum.contracts.commands import (
    AdjustVolume,
    CommandStatus,
    LedPattern,
    Pause,
    PlayAudio,
    SetLed,
    SetVolume,
    Speak,
)
from factotum.device.hands import DeviceHands

SPEAKER_HANDS = frozenset({Capability.AUDIO_IN, Capability.AUDIO_OUT})


def hands_with(speaker: Speaker | None) -> tuple[DeviceHands, RecordingRenderer]:
    shown = RecordingRenderer()
    return DeviceHands(SPEAKER_HANDS, speaker, shown), shown


class TestTheVolume:
    async def test_it_is_the_volume_of_the_speaker_and_is_said(self) -> None:
        speaker = Speaker(FakeBackend(), volume=100)
        hands, shown = hands_with(speaker)

        result = await hands.execute(SetVolume(level=30))

        assert result.succeeded
        assert speaker.volume == 30
        assert shown.of("event") == ["volume 30"]

    async def test_a_volume_of_nothing_is_a_speaker_that_is_silent(self) -> None:
        speaker = Speaker(FakeBackend())
        hands, _ = hands_with(speaker)

        await hands.execute(SetVolume(level=0))

        assert speaker.volume == 0

    async def test_a_device_with_no_speaker_has_no_volume_to_set(self) -> None:
        hands, shown = hands_with(None)

        result = await hands.execute(SetVolume(level=30))

        assert result.status is CommandStatus.UNSUPPORTED
        assert "set_volume" in (result.detail or "")
        assert shown.of("event") == []

    async def test_it_turns_the_volume_of_the_speaker_up_or_down_and_says_where_it_is(self) -> None:
        speaker = Speaker(FakeBackend(), volume=50)
        hands, shown = hands_with(speaker)

        up = await hands.execute(AdjustVolume(delta=10))
        down = await hands.execute(AdjustVolume(delta=-30))

        assert up.succeeded
        assert down.succeeded
        assert speaker.volume == 30
        assert shown.of("event") == ["volume 60", "volume 30"]

    async def test_a_change_never_takes_the_volume_past_either_end_and_the_level_said_is_real(
        self,
    ) -> None:
        speaker = Speaker(FakeBackend(), volume=95)
        hands, shown = hands_with(speaker)

        await hands.execute(AdjustVolume(delta=20))
        await hands.execute(AdjustVolume(delta=-100))

        assert speaker.volume == 0
        assert shown.of("event") == ["volume 100", "volume 0"]

    async def test_a_device_with_no_speaker_has_no_volume_to_change_either(self) -> None:
        hands, shown = hands_with(None)

        result = await hands.execute(AdjustVolume(delta=10))

        assert result.status is CommandStatus.UNSUPPORTED
        assert "adjust_volume" in (result.detail or "")
        assert shown.of("event") == []


class TestWhatItCannotDo:
    async def test_what_is_said_is_not_for_the_hands_and_the_skill_is_told_the_truth(self) -> None:
        # The clip is played by the client core and a text is made a clip by the edge: if either
        # reached these hands, saying that it was done would be an announcement that nobody heard.
        hands, shown = hands_with(Speaker(FakeBackend()))
        clip = PlayAudio(text="Hi.", audio=wav_of(bytes(100), 22_050))

        for command in (Speak(text="dinner is ready"), clip):
            result = await hands.execute(command)

            assert result.status is CommandStatus.UNSUPPORTED
            assert command.name in (result.detail or "")
        assert shown.of("speech") == []  # a text that was shown is not an announcement
        assert shown.of("event") == []

    async def test_a_command_for_hands_it_has_not_is_unsupported_and_says_which(self) -> None:
        hands, _ = hands_with(Speaker(FakeBackend()))

        for command in (Pause(), SetLed(pattern=LedPattern.BLINK)):
            result = await hands.execute(command)

            assert result.status is CommandStatus.UNSUPPORTED
            assert command.name in (result.detail or "")

    async def test_it_stands_for_what_it_was_given(self) -> None:
        hands, _ = hands_with(None)

        assert hands.capabilities == SPEAKER_HANDS
