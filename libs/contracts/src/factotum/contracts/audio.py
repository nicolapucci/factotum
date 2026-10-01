"""A clip of audio, carried whole.

This is the shape of a recording in every direction: a device's microphone to the edge, the edge
to the model gateway and back, the gateway's answer to the edge, and the edge to a device's
speaker. A future slice streams audio over the edge's WebSocket instead of sending one clip at a
time, and lets a device's own speech interrupt what it is playing (ADR 0021); this one is a single
clip, the same shape everywhere, like an ``Utterance`` is a single line of text.
"""

from enum import StrEnum

from pydantic import ConfigDict, Field

from factotum.contracts.base import Contract

MAX_AUDIO_BYTES = 2 * 1024 * 1024
"""About a minute of 16 kHz mono 16-bit audio: generous for one recording, not for a stream."""


class AudioFormat(StrEnum):
    WAV = "wav"
    """16-bit PCM, mono, in a WAV container: what every speech library here reads or writes."""


class AudioPayload(Contract):
    """A clip of audio. ``data`` is bytes, carried as base64 in JSON."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
        ser_json_bytes="base64",
        val_json_bytes="base64",
    )

    format: AudioFormat = AudioFormat.WAV
    sample_rate: int = Field(ge=8000, le=48000)
    data: bytes = Field(min_length=1, max_length=MAX_AUDIO_BYTES)
