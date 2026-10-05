"""Text-to-speech with Piper, in the gateway's own process: no address, no key.

A binding's ``model`` is a Piper voice name (``"it_IT-paola-medium"``...) or a path to a voice's
``.onnx`` file already downloaded. The voice for a name is loaded the first time it is asked to
synthesize, not at startup, so a deployment that never binds the ``tts`` role to this provider
never loads one. Loading a name that is not already a file downloads it from Hugging Face; point
``cache_dir`` at a directory of your own to control where and to look for what was downloaded
before (``FACTOTUM_GATEWAY_SPEECH_CACHE_DIR``).
"""

import asyncio
import io
import wave
from collections.abc import AsyncGenerator, Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, Protocol

import httpx

from factotum.contracts.audio import AudioFormat, AudioPayload
from factotum.contracts.chat import ChatRequest, ChatResult, EventUnion
from factotum.contracts.errors import ProviderUnavailableError
from factotum.contracts.models import ProviderKind
from factotum.model_gateway.providers.base import (
    EmbeddingBatch,
    ResolvedBinding,
    SpeechPiece,
    role_not_supported,
)

_DEFAULT_CACHE_DIR = Path.home() / ".cache" / "factotum" / "piper"


class PiperEngine(Protocol):
    """What the adapter needs of Piper: built once per voice, asked to speak many times."""

    def synthesize_wav(self, text: str) -> bytes:
        """A whole WAV file (header included), so the sample rate travels with it."""
        ...

    def synthesize_pieces(self, text: str) -> Iterator[SpeechPiece]:
        """What the voice makes of ``text``, a sentence at a time, each as it is made. Runs on the
        calling thread, one piece each time it is asked for: callers advance it with
        ``asyncio.to_thread``."""
        ...


EngineFactory = Callable[[str], PiperEngine]
"""Builds the engine for a binding's model (voice name). Tests pass a fake instead of the default
(``LocalPiperEngine``) so they never load a real voice."""


class LocalPiperEngine:
    """Loads a ``piper.PiperVoice`` the first time it is asked to synthesize, never at
    construction: importing this module must not pull in ``piper`` (and onnxruntime) for a
    deployment that never uses it."""

    def __init__(self, voice: str, *, cache_dir: Path | None, use_cuda: bool) -> None:
        self._voice = voice
        self._cache_dir = cache_dir or _DEFAULT_CACHE_DIR
        self._use_cuda = use_cuda
        self._loaded: Any | None = None

    def synthesize_wav(self, text: str) -> bytes:
        """Runs on the calling thread: callers run it with ``asyncio.to_thread``."""
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            self._loaded_voice().synthesize_wav(text, wav_file)
        return buffer.getvalue()

    def synthesize_pieces(self, text: str) -> Iterator[SpeechPiece]:
        """Piper makes one chunk of audio for each sentence of ``text``. The text is turned into
        phonemes when the first is asked for, and each sentence is made when its turn comes."""
        for chunk in self._loaded_voice().synthesize(text):
            yield SpeechPiece(sample_rate=chunk.sample_rate, pcm=chunk.audio_int16_bytes)

    def _loaded_voice(self) -> Any:
        if self._loaded is None:
            from piper import PiperVoice  # heavy: deferred to the first real use

            self._loaded = PiperVoice.load(str(self._onnx_path()), use_cuda=self._use_cuda)
        return self._loaded

    def _onnx_path(self) -> Path:
        given = Path(self._voice)
        if given.suffix == ".onnx":
            return given  # a path of the caller's own; downloading is not this adapter's business
        onnx_path = self._cache_dir / f"{self._voice}.onnx"
        if not onnx_path.exists():
            from piper.download_voices import download_voice  # heavy: deferred

            self._cache_dir.mkdir(parents=True, exist_ok=True)
            download_voice(self._voice, self._cache_dir)
        return onnx_path


class PiperLocalAdapter:
    """Text-to-speech only: chat and embeddings are not what this is for."""

    kind = ProviderKind.PIPER_LOCAL

    def __init__(
        self,
        *,
        use_cuda: bool = False,
        cache_dir: Path | None = None,
        engine_factory: EngineFactory | None = None,
    ) -> None:
        self._engine_factory: EngineFactory = engine_factory or (
            lambda voice: LocalPiperEngine(voice, cache_dir=cache_dir, use_cuda=use_cuda)
        )
        self._engines: dict[str, PiperEngine] = {}

    def native_structured_output(self, resolved: ResolvedBinding) -> bool:
        return False

    # --- not what this provider is for ---

    async def chat(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, request: ChatRequest
    ) -> ChatResult:
        raise role_not_supported(self.kind, "chat")

    async def chat_stream(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, request: ChatRequest
    ) -> AsyncGenerator[EventUnion]:
        raise role_not_supported(self.kind, "chat")
        yield  # pragma: no cover - never reached; keeps this an async generator function

    async def embed(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, inputs: Sequence[str]
    ) -> EmbeddingBatch:
        raise role_not_supported(self.kind, "embeddings")

    async def transcribe(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, audio: AudioPayload
    ) -> str:
        raise role_not_supported(self.kind, "speech-to-text")

    # --- text-to-speech ---

    async def synthesize(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, text: str
    ) -> AudioPayload:
        engine = self._engine_for(resolved.model)
        try:
            wav_bytes = await asyncio.to_thread(engine.synthesize_wav, text)
        except Exception as error:
            raise self._could_not_synthesize(error) from error
        with wave.open(io.BytesIO(wav_bytes)) as wav_file:
            sample_rate = wav_file.getframerate()
        return AudioPayload(format=AudioFormat.WAV, sample_rate=sample_rate, data=wav_bytes)

    async def synthesize_stream(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, text: str
    ) -> AsyncGenerator[SpeechPiece]:
        """A sentence at a time. Each is made on a thread of its own, so that what is waiting for
        the next one (the answer of a device that is talking) is not held while it is made, and the
        voice is not asked for another once whoever listens has gone."""
        engine = self._engine_for(resolved.model)
        try:
            pieces = iter(engine.synthesize_pieces(text))
        except Exception as error:
            raise self._could_not_synthesize(error) from error
        while True:
            try:
                piece = await asyncio.to_thread(next, pieces, None)
            except Exception as error:
                raise self._could_not_synthesize(error) from error
            if piece is None:
                return
            yield piece

    def _could_not_synthesize(self, error: Exception) -> ProviderUnavailableError:
        return ProviderUnavailableError(
            f"{self.kind.value}: could not synthesize ({type(error).__name__})",
            details={"provider": self.kind.value},
        )

    def _engine_for(self, voice: str) -> PiperEngine:
        if voice not in self._engines:
            self._engines[voice] = self._engine_factory(voice)
        return self._engines[voice]
