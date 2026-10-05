"""Audio as it crosses the system: a clip carried whole, and the samples of a stream.

A clip is the shape of a recording in every direction: a device's microphone to the edge (an
``audio_utterance``), the edge to the model gateway and back, the gateway's answer to the edge and
the edge to a device's speaker. A stream is raw samples sent as they are made, in both directions: a
device sends the edge what a person says while they are still talking (``audio_start``,
``audio_chunk``, ``audio_end``: ADR 0022), which the edge puts into a clip once the person has
stopped; and the model gateway and then the edge send what the assistant says while it is still
being made (``synthesis`` events, then ``voice_start``, ``voice_chunk``, ``voice_end``: ADR 0023).
A device that does not take a voice as a stream is still sent a clip for each sentence.
"""

import io
import wave
from collections.abc import Iterator
from enum import StrEnum

from pydantic import ConfigDict, Field

from factotum.contracts.base import Contract

MIN_SAMPLE_RATE = 8000
MAX_SAMPLE_RATE = 48000

MAX_AUDIO_BYTES = 2 * 1024 * 1024
"""About a minute of 16 kHz mono 16-bit audio: the most one recording carries, whole in a clip or
as a stream (what a model is given is always a clip)."""

WAV_HEADER_BYTES = 44
"""What the WAV container adds to its samples."""

MAX_STREAM_BYTES = MAX_AUDIO_BYTES - WAV_HEADER_BYTES
"""The most samples a stream may hold: put in a WAV file they still make a clip that is within
``MAX_AUDIO_BYTES``."""

MAX_CHUNK_BYTES = 64 * 1024
"""The most samples one chunk of a stream carries: two seconds at 16 kHz. A device sends 100 to
200 ms at a time."""

VOICE_CHUNK_MS = 100
"""How much sound a chunk of a voice carries when the model gateway cuts what a voice made into
chunks: little enough that a device can start to play it at once and that stopping loses nothing,
and much more than the overhead of a frame."""

BYTES_PER_SAMPLE = 2
"""Of ``StreamFormat.PCM_S16LE``: 16 bits, one channel."""


class AudioFormat(StrEnum):
    WAV = "wav"
    """16-bit PCM, mono, in a WAV container: what every speech library here reads or writes."""


class StreamFormat(StrEnum):
    """What the samples of a stream are."""

    PCM_S16LE = "pcm_s16le"
    """16-bit signed little-endian samples, one channel, no container: what a microphone hands
    over, and what ``wav_of`` puts in a WAV file."""


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
    sample_rate: int = Field(ge=MIN_SAMPLE_RATE, le=MAX_SAMPLE_RATE)
    data: bytes = Field(min_length=1, max_length=MAX_AUDIO_BYTES)


def wav_of(pcm: bytes, sample_rate: int) -> AudioPayload:
    """Samples of ``StreamFormat.PCM_S16LE`` as a clip. A sample cut in half at the end is left
    out: a person's last millisecond is not worth a header that says otherwise."""
    whole = pcm[: len(pcm) - len(pcm) % 2]
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(whole)
    return AudioPayload(format=AudioFormat.WAV, sample_rate=sample_rate, data=buffer.getvalue())


def pcm_of(wav: bytes) -> tuple[bytes, int]:
    """The samples and the sample rate of a WAV file, the other way from ``wav_of``. Only 16-bit
    mono PCM is what a stream carries: anything else is a ``ValueError``."""
    try:
        with wave.open(io.BytesIO(wav), "rb") as wav_file:
            if wav_file.getnchannels() != 1 or wav_file.getsampwidth() != 2:
                raise ValueError("only 16-bit mono audio can be streamed")
            return wav_file.readframes(wav_file.getnframes()), wav_file.getframerate()
    except (wave.Error, EOFError) as error:
        raise ValueError(f"this is not a WAV file of 16-bit PCM: {error}") from None


def whole_samples(data: bytes) -> bytes:
    """``data`` when it holds whole samples, a ``ValueError`` when it ends in the middle of one: a
    chunk of a voice is whole samples, so that what plays it never has half of one to carry over."""
    if len(data) % BYTES_PER_SAMPLE:
        raise ValueError("a chunk carries whole 16-bit samples")
    return data


def chunk_bytes(sample_rate: int, milliseconds: int = VOICE_CHUNK_MS) -> int:
    """How many bytes of samples last ``milliseconds`` at ``sample_rate``, in whole samples and at
    least one of them."""
    samples = max(1, sample_rate * milliseconds // 1000)
    return samples * BYTES_PER_SAMPLE


def slices(pcm: bytes, size: int) -> Iterator[bytes]:
    """``pcm`` in pieces of at most ``size`` bytes, in order, which put together are ``pcm`` again.
    ``size`` is a whole number of samples (``chunk_bytes`` gives one), so no piece cuts a sample
    in half unless ``pcm`` itself does."""
    if size < 1:
        raise ValueError("a piece holds at least one byte")
    for start in range(0, len(pcm), size):
        yield pcm[start : start + size]
