from typing import Any

import pytest
from pydantic import AnyHttpUrl, TypeAdapter, ValidationError

from factotum.contracts.audio import AudioFormat, AudioPayload
from factotum.contracts.capabilities import Capability
from factotum.contracts.cards import Card
from factotum.contracts.commands import LedPattern, Pause, SetLed, SetVolume, Speak
from factotum.contracts.response import (
    CardPart,
    CommandPart,
    MediaPart,
    PartUnion,
    ResponsePart,
    Speech,
    StatusPart,
    StatusState,
    Text,
    required_capability,
)

part_adapter: TypeAdapter[Any] = TypeAdapter(ResponsePart)

CASES: list[tuple[PartUnion, Capability | None]] = [
    (Speech(text="Ecco la ricetta"), Capability.AUDIO_OUT),
    (Text(text="Ecco la ricetta"), Capability.DISPLAY),
    (CardPart(card=Card(title="Ricetta")), Capability.DISPLAY),
    (MediaPart(url=AnyHttpUrl("https://radio.example.org/stream")), Capability.MEDIA_PLAYER),
    (CommandPart(command=SetVolume(level=20)), Capability.AUDIO_OUT),
    (CommandPart(command=Pause()), Capability.MEDIA_PLAYER),
    (CommandPart(command=SetLed(pattern=LedPattern.BLINK)), Capability.LED),
    (StatusPart(state=StatusState.THINKING), None),
]


@pytest.mark.parametrize(("part", "capability"), CASES, ids=lambda x: type(x).__name__)
def test_each_part_declares_the_capability_a_device_needs_to_render_it(
    part: PartUnion, capability: Capability | None
) -> None:
    assert required_capability(part) is capability


def test_a_command_part_asks_for_whatever_its_command_asks_for() -> None:
    part = CommandPart(command=Speak(text="Ciao"))

    assert required_capability(part) is part.command.requires


@pytest.mark.parametrize(("part", "_"), CASES, ids=lambda x: type(x).__name__)
def test_a_part_round_trips_through_the_discriminated_union(
    part: PartUnion, _: Capability | None
) -> None:
    restored = part_adapter.validate_json(part.model_dump_json())

    assert restored == part
    assert type(restored) is type(part)


def test_the_type_selects_the_part() -> None:
    part = part_adapter.validate_python({"type": "status", "state": "searching"})

    assert isinstance(part, StatusPart)


def test_an_unknown_part_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        part_adapter.validate_python({"type": "hologram"})


def test_speech_cannot_be_empty() -> None:
    with pytest.raises(ValidationError):
        Speech(text="")


def test_speech_has_no_audio_until_something_synthesizes_it() -> None:
    assert Speech(text="Ecco la ricetta").audio is None


def test_speech_with_audio_still_needs_a_speaker_and_round_trips() -> None:
    payload = AudioPayload(format=AudioFormat.WAV, sample_rate=16000, data=b"some-wav-bytes")
    part = Speech(text="Ecco la ricetta", audio=payload)

    assert required_capability(part) is Capability.AUDIO_OUT
    assert part_adapter.validate_json(part.model_dump_json()) == part


def test_a_media_part_needs_a_valid_url() -> None:
    with pytest.raises(ValidationError):
        MediaPart.model_validate({"url": "not a url"})
