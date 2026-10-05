"""Helpers for testing adapters and the gateway without a network."""

import io
import json
import math
import struct
import wave
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping, Sequence
from typing import Any, Self

import httpx
from pydantic import SecretStr

from factotum.contracts.audio import AudioPayload
from factotum.contracts.chat import ChatMessage, ChatRequest, ChatResult, EventUnion, MessageRole
from factotum.contracts.errors import FactotumError
from factotum.contracts.models import ModelBinding, ProviderKind
from factotum.model_gateway.providers.base import EmbeddingBatch, ResolvedBinding

Handler = Callable[[httpx.Request], httpx.Response]


def mock_client(handler: Handler) -> httpx.AsyncClient:
    """An httpx client whose "network" is ``handler``."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def resolved(
    binding: ModelBinding, *, base_url: str | None = None, api_key: str | None = None
) -> ResolvedBinding:
    """A resolved binding, the way the gateway would build it."""
    address = base_url or binding.base_url or "https://provider.example.org"
    return ResolvedBinding(
        binding=binding,
        base_url=address.rstrip("/"),
        api_key=SecretStr(api_key) if api_key else None,
    )


def json_response(
    body: Any, *, status: int = 200, headers: Mapping[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(status, json=body, headers=dict(headers or {}))


def sse_body(*messages: tuple[str | None, Any]) -> bytes:
    """A server-sent events body. Each message is ``(event name or None, data)``; a data that is
    not a string is sent as JSON."""
    chunks: list[str] = []
    for event, data in messages:
        lines = [f"event: {event}"] if event else []
        payload = data if isinstance(data, str) else json.dumps(data)
        lines.append(f"data: {payload}")
        chunks.append("\n".join(lines))
    return ("\n\n".join(chunks) + "\n\n").encode()


def ndjson_body(*items: Any) -> bytes:
    """A newline-delimited JSON body."""
    return ("\n".join(json.dumps(item) for item in items) + "\n").encode()


def sse_response(*messages: tuple[str | None, Any], status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, content=sse_body(*messages), headers={"content-type": "text/event-stream"}
    )


def ndjson_response(*items: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, content=ndjson_body(*items), headers={"content-type": "application/x-ndjson"}
    )


class FakeServer:
    """A provider that answers as told, one response per request, and keeps what it received."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = list(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responses.pop(0)

    @property
    def client(self) -> httpx.AsyncClient:
        return mock_client(self)

    @property
    def request(self) -> httpx.Request:
        """The last request received."""
        return self.requests[-1]

    @property
    def sent(self) -> dict[str, Any]:
        """The JSON body of the last request."""
        body: dict[str, Any] = json.loads(self.request.content)
        return body


def a_tone_wav(rate: int, *, seconds: float = 0.5, channels: int = 1) -> bytes:
    """A tone, as a WAV file at the sample rate of a microphone or of a voice: what matters to a
    test of what reads a recording is that the file is a real one."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(rate)
        for n in range(int(rate * seconds)):
            sample = struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * n / rate)))
            wav_file.writeframes(sample * channels)
    return buffer.getvalue()


def ask(text: str = "hello", **fields: Any) -> ChatRequest:
    """A chat request with one user message."""
    return ChatRequest(messages=(ChatMessage(role=MessageRole.USER, content=text),), **fields)


class ScriptedAdapter:
    """A provider adapter that does as told, for testing what is built on top of adapters.

    Each ``will_*`` call queues what the next call does: an answer to return, or an error to raise.
    A stream is queued as a sequence of events and errors. Everything the adapter was asked is kept.
    """

    def __init__(self, kind: ProviderKind = ProviderKind.OLLAMA, *, native: bool = True) -> None:
        self.kind = kind
        self.native = native
        self.chats: list[tuple[ResolvedBinding, ChatRequest]] = []
        self.streams: list[tuple[ResolvedBinding, ChatRequest]] = []
        self.embeds: list[tuple[ResolvedBinding, tuple[str, ...]]] = []
        self.transcriptions: list[tuple[ResolvedBinding, AudioPayload]] = []
        self.syntheses: list[tuple[ResolvedBinding, str]] = []
        self.stream_endings: list[str] = []
        self._chat_script: list[ChatResult | Exception] = []
        self._stream_script: list[Sequence[EventUnion | Exception]] = []
        self._embed_script: list[EmbeddingBatch | Exception] = []
        self._transcribe_script: list[str | Exception] = []
        self._synthesize_script: list[AudioPayload | Exception] = []

    def will_chat(self, *outcomes: ChatResult | Exception) -> Self:
        self._chat_script.extend(outcomes)
        return self

    def will_stream(self, *items: EventUnion | Exception) -> Self:
        self._stream_script.append(items)
        return self

    def will_embed(self, *outcomes: EmbeddingBatch | Exception) -> Self:
        self._embed_script.extend(outcomes)
        return self

    def will_transcribe(self, *outcomes: str | Exception) -> Self:
        self._transcribe_script.extend(outcomes)
        return self

    def will_synthesize(self, *outcomes: AudioPayload | Exception) -> Self:
        self._synthesize_script.extend(outcomes)
        return self

    def native_structured_output(self, resolved: ResolvedBinding) -> bool:
        return self.native

    async def chat(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, request: ChatRequest
    ) -> ChatResult:
        self.chats.append((resolved, request))
        outcome = self._chat_script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def chat_stream(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, request: ChatRequest
    ) -> AsyncGenerator[EventUnion]:
        self.streams.append((resolved, request))
        ending = "closed"
        try:
            for item in self._stream_script.pop(0):
                if isinstance(item, Exception):
                    ending = "failed"
                    raise item
                yield item
            ending = "completed"
        finally:
            self.stream_endings.append(ending)

    async def embed(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, inputs: Sequence[str]
    ) -> EmbeddingBatch:
        self.embeds.append((resolved, tuple(inputs)))
        outcome = self._embed_script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def transcribe(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, audio: AudioPayload
    ) -> str:
        self.transcriptions.append((resolved, audio))
        outcome = self._transcribe_script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def synthesize(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, text: str
    ) -> AudioPayload:
        self.syntheses.append((resolved, text))
        outcome = self._synthesize_script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class TrackedClients:
    """A client factory whose clients cannot reach any network, and that remembers them, so that a
    test can check they were closed."""

    def __init__(self) -> None:
        self.created: list[httpx.AsyncClient] = []

    def __call__(self, resolved: ResolvedBinding) -> httpx.AsyncClient:
        client = mock_client(_unreachable)
        self.created.append(client)
        return client


def _unreachable(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected request to {request.url}")


class Sleeps:
    """A stand-in for ``asyncio.sleep``: it does not wait, only remembers the delays asked for."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


async def collect_events(
    stream: AsyncIterator[EventUnion],
) -> tuple[list[EventUnion], FactotumError | None]:
    """Everything a stream delivered, and the error that ended it, if one did."""
    got: list[EventUnion] = []
    try:
        async for event in stream:
            got.append(event)  # noqa: PERF401 - what came before a failure is what is being kept
    except FactotumError as error:
        return got, error
    return got, None


async def events(stream: AsyncIterator[EventUnion]) -> list[EventUnion]:
    """Everything a stream delivered; the error that ended it, if any, is raised."""
    got, error = await collect_events(stream)
    if error is not None:
        raise error
    return got


__all__ = [
    "FakeServer",
    "Handler",
    "ScriptedAdapter",
    "Sleeps",
    "TrackedClients",
    "a_tone_wav",
    "ask",
    "collect_events",
    "events",
    "json_response",
    "mock_client",
    "ndjson_body",
    "ndjson_response",
    "resolved",
    "sse_body",
    "sse_response",
]
