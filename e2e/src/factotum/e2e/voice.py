"""A voice, made up: what a microphone hands over and what a speaker plays, and the engines of
Whisper and Piper that stand behind the real adapters of the gateway.

A recording is a WAV file whose samples are the bytes of the words that were said in it. The
stand-in of Whisper reads the words back out of a recording, and the stand-in of Piper writes a clip
that carries the words it was asked to say, so a test can tell what a speaker was made to say. The
real engines (faster-whisper, Piper) are what these are not: they decide what a recording says and
how a text sounds, which is theirs to test.
"""

import io
import struct
import wave
from typing import Protocol

from factotum.contracts.audio import AudioPayload


class WhisperDouble(Protocol):
    """What a test looks at of the engine behind ``whisper_local``, whether it is the stand-in below
    or the real one under a recorder (``factotum.e2e.speech``)."""

    models: list[str]
    heard: list[str]
    languages: list[tuple[str, ...] | None]
    broken: bool

    def build(self, model: str) -> "WhisperDouble": ...

    def transcribe_wav(self, wav_bytes: bytes, languages: tuple[str, ...] | None) -> str: ...


class PiperDouble(Protocol):
    """The same of the engine behind ``piper_local``."""

    voices: list[str]
    spoken: list[str]

    def build(self, voice: str) -> "PiperDouble": ...

    def synthesize_wav(self, text: str) -> bytes: ...


def wav_saying(words: str, rate: int, *, seconds: float = 0.0) -> bytes:
    """A WAV file whose samples are the bytes of ``words``, and silence after them (a little, or
    enough for it to last ``seconds``): a voice that can be read back."""
    spoken = words.encode()
    silence = max(160, round(rate * seconds) - len(spoken))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(rate)
        file.writeframes(struct.pack(f"<{len(spoken)}h", *spoken) + bytes(2 * silence))
    return buffer.getvalue()


def words_in(wav: bytes) -> str:
    with wave.open(io.BytesIO(wav)) as file:
        frames = file.readframes(file.getnframes())
    samples = struct.unpack(f"<{len(frames) // 2}h", frames)
    return bytes(samples).rstrip(b"\x00").decode()


def recording_of(words: str, *, seconds: float = 0.0) -> AudioPayload:
    """What a microphone hands over after somebody said ``words``; no words is a silence."""
    return AudioPayload(sample_rate=16_000, data=wav_saying(words, 16_000, seconds=seconds))


def said_in(clip: AudioPayload) -> str:
    return words_in(clip.data)


class StandInWhisper:
    """The engine of ``whisper_local``: it reads the words out of a recording made by
    ``recording_of``."""

    def __init__(self) -> None:
        self.models: list[str] = []
        """The model sizes the adapter asked for: what a binding said reaches the engine."""

        self.heard: list[str] = []
        self.languages: list[tuple[str, ...] | None] = []
        """The languages the binding allowed, for each recording: what a binding said reaches the
        engine, through the hub as well when the hub holds the binding."""

        self.broken = False

    def build(self, model: str) -> "StandInWhisper":
        self.models.append(model)
        return self

    def transcribe_wav(self, wav_bytes: bytes, languages: tuple[str, ...] | None) -> str:
        if self.broken:
            raise RuntimeError("the model fell over")
        self.languages.append(languages)
        self.heard.append(words_in(wav_bytes))
        return self.heard[-1]


class StandInPiper:
    """The engine of ``piper_local``: it writes a clip that carries the words it was asked for."""

    def __init__(self) -> None:
        self.voices: list[str] = []
        self.spoken: list[str] = []

    def build(self, voice: str) -> "StandInPiper":
        self.voices.append(voice)
        return self

    def synthesize_wav(self, text: str) -> bytes:
        self.spoken.append(text)
        return wav_saying(text, 22_050)
