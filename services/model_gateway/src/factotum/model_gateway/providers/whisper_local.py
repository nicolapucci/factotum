"""Speech-to-text with ``faster-whisper``, in the gateway's own process: no address, no key.

A binding's ``model`` is a Whisper model size (``"base"``, ``"small"``, ``"large-v3"``...) or a
path to one already downloaded. The engine for a size is built the first time that size is asked
to transcribe, not at startup, so a deployment that never binds the ``stt`` role to this provider
never loads one. Building it loads ``faster_whisper.WhisperModel``, which downloads the model from
Hugging Face the first time that size has not already been cached; point ``cache_dir`` at a
directory of your own to control where (``FACTOTUM_GATEWAY_SPEECH_CACHE_DIR``).
"""

import asyncio
import io
from collections.abc import AsyncGenerator, Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

import httpx

from factotum.contracts.audio import AudioPayload
from factotum.contracts.chat import ChatRequest, ChatResult, EventUnion
from factotum.contracts.errors import ProviderUnavailableError
from factotum.contracts.models import ProviderKind
from factotum.model_gateway.providers.base import (
    EmbeddingBatch,
    ResolvedBinding,
    role_not_supported,
)


class WhisperEngine(Protocol):
    """What the adapter needs of faster-whisper: built once per model size, asked for text many
    times."""

    def transcribe_wav(self, wav_bytes: bytes) -> str: ...


EngineFactory = Callable[[str], WhisperEngine]
"""Builds the engine for a binding's model size. Tests pass a fake instead of the default
(``FasterWhisperEngine``) so they never load a real model."""


class FasterWhisperEngine:
    """Loads a ``faster_whisper.WhisperModel`` the first time it is asked to transcribe, never at
    construction: importing this module must not pull in ``faster_whisper`` (and the GPU or CPU
    inference stack that comes with it) for a deployment that never uses it."""

    def __init__(self, model_size: str, *, device: str, cache_dir: Path | None) -> None:
        self._model_size = model_size
        self._device = device
        self._cache_dir = cache_dir
        self._model: Any | None = None

    def transcribe_wav(self, wav_bytes: bytes) -> str:
        """Runs on the calling thread: callers run it with ``asyncio.to_thread``."""
        segments, _ = self._loaded().transcribe(io.BytesIO(wav_bytes))
        return " ".join(segment.text.strip() for segment in segments).strip()

    def _loaded(self) -> Any:
        if self._model is None:
            from faster_whisper import WhisperModel  # heavy: deferred to the first real use

            compute_type = "int8" if self._device == "cpu" else "float16"
            self._model = WhisperModel(
                self._model_size,
                device=self._device,
                compute_type=compute_type,
                download_root=str(self._cache_dir) if self._cache_dir else None,
            )
        return self._model


class WhisperLocalAdapter:
    """Speech-to-text only: chat, streaming and embeddings are not what this is for."""

    kind = ProviderKind.WHISPER_LOCAL

    def __init__(
        self,
        *,
        device: str = "cpu",
        cache_dir: Path | None = None,
        engine_factory: EngineFactory | None = None,
    ) -> None:
        self._engine_factory: EngineFactory = engine_factory or (
            lambda model_size: FasterWhisperEngine(model_size, device=device, cache_dir=cache_dir)
        )
        self._engines: dict[str, WhisperEngine] = {}

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

    async def synthesize(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, text: str
    ) -> AudioPayload:
        raise role_not_supported(self.kind, "text-to-speech")

    # --- speech-to-text ---

    async def transcribe(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, audio: AudioPayload
    ) -> str:
        engine = self._engine_for(resolved.model)
        try:
            return await asyncio.to_thread(engine.transcribe_wav, audio.data)
        except Exception as error:
            raise ProviderUnavailableError(
                f"{self.kind.value}: could not transcribe ({type(error).__name__})",
                details={"provider": self.kind.value},
            ) from error

    def _engine_for(self, model_size: str) -> WhisperEngine:
        if model_size not in self._engines:
            self._engines[model_size] = self._engine_factory(model_size)
        return self._engines[model_size]
