"""OpenAI, and every server that speaks its Chat Completions API.

One adapter serves both: they share the wire format and differ in a few defaults. OpenAI takes
``max_completion_tokens`` and constrains the output to a schema itself. The compatible servers
(vLLM, LM Studio, llama.cpp, OpenRouter, Groq...) take the older ``max_tokens`` and get structured
output emulated, unless the user declares that theirs supports it: many accept the request and
ignore the schema.

The address is the API root, ``/v1`` included: compatible servers put it in different places.
"""

from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import Field

from factotum.contracts.audio import AudioPayload
from factotum.contracts.chat import (
    ChatRequest,
    ChatResult,
    EventUnion,
    FinishEvent,
    FinishReason,
    TextDelta,
    Usage,
    UsageEvent,
)
from factotum.contracts.errors import (
    ContextTooLongError,
    ProviderAuthError,
    ProviderError,
    ProviderUnavailableError,
)
from factotum.contracts.models import ProviderKind
from factotum.model_gateway.providers.base import (
    EmbeddingBatch,
    ResolvedBinding,
    SpeechPiece,
    WireModel,
    api_url,
    bearer_headers,
    effective_max_output_tokens,
    effective_temperature,
    error_body,
    error_for_status,
    iter_sse,
    parse_sse_json,
    parse_wire,
    post_json,
    role_not_supported,
    stream_error,
    stream_post,
    usage_of,
)
from factotum.model_gateway.structured import strict_compatible

_KINDS = frozenset({ProviderKind.OPENAI, ProviderKind.OPENAI_COMPATIBLE})

_CONTEXT_CODES = frozenset({"context_length_exceeded", "exceed_context_size_error"})
_CONTEXT_PHRASES = ("maximum context length", "exceeds the available context size")

_FINISH_REASONS = {
    "stop": FinishReason.STOP,
    "length": FinishReason.LENGTH,
    "content_filter": FinishReason.CONTENT_FILTER,
}


# --- what the API sends ---


class _WireUsage(WireModel):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class _Message(WireModel):
    content: str | None = None
    refusal: str | None = None


class _Choice(WireModel):
    message: _Message
    finish_reason: str | None = None


class _Completion(WireModel):
    choices: list[_Choice] = Field(min_length=1)
    usage: _WireUsage | None = None


class _Delta(WireModel):
    content: str | None = None
    refusal: str | None = None


class _StreamChoice(WireModel):
    delta: _Delta = Field(default_factory=_Delta)
    finish_reason: str | None = None


class _Chunk(WireModel):
    """A streamed chunk. With ``include_usage`` the last one has no choices, only the usage."""

    choices: list[_StreamChoice] = Field(default_factory=list)
    usage: _WireUsage | None = None


class _EmbeddingItem(WireModel):
    index: int | None = None
    embedding: list[float]


class _EmbeddingsBody(WireModel):
    data: list[_EmbeddingItem]
    usage: _WireUsage | None = None


@dataclass(frozen=True, slots=True)
class _ErrorInfo:
    message: str
    code: str | None
    type: str | None

    @property
    def provider_code(self) -> str | None:
        return self.code or self.type


def _first_str(*values: object) -> str | None:
    return next((value for value in values if isinstance(value, str) and value), None)


def _error_info(body: Any) -> _ErrorInfo | None:
    """The error in a body, in OpenAI's shape ``{"error": {...}}`` or the plainer ones compatible
    servers use (``{"message": ...}``, ``{"detail": ...}``)."""
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if isinstance(error, dict):
        return _ErrorInfo(
            _first_str(error.get("message")) or "",
            _first_str(error.get("code")),
            _first_str(error.get("type")),
        )
    if isinstance(error, str):
        return _ErrorInfo(error, None, None)
    plain = _first_str(body.get("message"), body.get("detail"))
    return _ErrorInfo(plain, None, None) if plain else None


def _usage(wire: _WireUsage | None) -> Usage | None:
    if wire is None:
        return None
    return usage_of(wire.prompt_tokens, wire.completion_tokens)


def _finish_reason(raw: str | None) -> FinishReason:
    return _FINISH_REASONS.get(raw or "", FinishReason.OTHER)


class OpenAIAdapter:
    def __init__(self, kind: ProviderKind = ProviderKind.OPENAI) -> None:
        if kind not in _KINDS:
            raise ValueError(f"{kind.value} does not speak the OpenAI API")
        self.kind = kind
        self._official = kind is ProviderKind.OPENAI

    def native_structured_output(self, resolved: ResolvedBinding) -> bool:
        declared = resolved.options.native_structured_output
        return self._official if declared is None else declared

    # --- chat ---

    async def chat(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, request: ChatRequest
    ) -> ChatResult:
        body = await post_json(
            client,
            api_url(resolved, "chat/completions"),
            headers=bearer_headers(resolved),
            payload=self._chat_payload(resolved, request, stream=False),
            provider=self.kind,
            map_error=self._map_error,
        )
        completion = parse_wire(_Completion, body, self.kind)
        choice = completion.choices[0]
        usage = _usage(completion.usage)
        if choice.message.refusal:
            return ChatResult(
                text=choice.message.refusal, finish_reason=FinishReason.CONTENT_FILTER, usage=usage
            )
        return ChatResult(
            text=choice.message.content or "",
            finish_reason=_finish_reason(choice.finish_reason),
            usage=usage,
        )

    async def chat_stream(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, request: ChatRequest
    ) -> AsyncGenerator[EventUnion]:
        """Text as it arrives, then the usage if the server reports it, then the finish, last."""
        finish: str | None = None
        refused = False
        done = False
        usage: Usage | None = None
        async with stream_post(
            client,
            api_url(resolved, "chat/completions"),
            headers=bearer_headers(resolved),
            payload=self._chat_payload(resolved, request, stream=True),
            provider=self.kind,
            map_error=self._map_error,
        ) as response:
            async for message in iter_sse(response):
                if message.data.strip() == "[DONE]":
                    done = True
                    break
                data = parse_sse_json(message, self.kind)
                self._raise_reported_error(data)
                chunk = parse_wire(_Chunk, data, self.kind)
                usage = _usage(chunk.usage) or usage
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if choice.delta.content:
                    yield TextDelta(text=choice.delta.content)
                if choice.delta.refusal:
                    refused = True
                    yield TextDelta(text=choice.delta.refusal)
                finish = choice.finish_reason or finish
        if finish is None and not done:
            raise ProviderUnavailableError(
                f"{self.kind.value}: the stream ended before the answer was complete",
                details={"provider": self.kind.value},
            )
        if usage is not None:
            yield UsageEvent(usage=usage)
        yield FinishEvent(reason=FinishReason.CONTENT_FILTER if refused else _finish_reason(finish))

    def _chat_payload(
        self, resolved: ResolvedBinding, request: ChatRequest, *, stream: bool
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": resolved.model,
            "messages": [
                {"role": message.role.value, "content": message.content}
                for message in request.messages
            ],
        }
        max_tokens = effective_max_output_tokens(resolved, request)
        if max_tokens is not None:
            payload["max_completion_tokens" if self._official else "max_tokens"] = max_tokens
        temperature = effective_temperature(resolved, request)
        if temperature is not None:
            payload["temperature"] = temperature
        if stream:
            payload["stream"] = True
            declared = resolved.options.stream_usage
            if self._official if declared is None else declared:
                payload["stream_options"] = {"include_usage": True}
        if request.response_schema is not None:
            payload["response_format"] = _response_format(request.response_schema)
        return payload

    # --- embeddings ---

    async def embed(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, inputs: Sequence[str]
    ) -> EmbeddingBatch:
        payload: dict[str, Any] = {"model": resolved.model, "input": list(inputs)}
        if resolved.options.dimensions is not None:
            payload["dimensions"] = resolved.options.dimensions
        body = await post_json(
            client,
            api_url(resolved, "embeddings"),
            headers=bearer_headers(resolved),
            payload=payload,
            provider=self.kind,
            map_error=self._map_error,
        )
        parsed = parse_wire(_EmbeddingsBody, body, self.kind)
        items = parsed.data
        if all(item.index is not None for item in items):
            items = sorted(items, key=lambda item: item.index or 0)
        return EmbeddingBatch(
            vectors=tuple(tuple(item.embedding) for item in items), usage=_usage(parsed.usage)
        )

    # --- speech ---

    async def transcribe(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, audio: AudioPayload
    ) -> str:
        raise role_not_supported(self.kind, "speech-to-text")

    async def synthesize(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, text: str
    ) -> AudioPayload:
        raise role_not_supported(self.kind, "text-to-speech")

    async def synthesize_stream(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, text: str
    ) -> AsyncGenerator[SpeechPiece]:
        raise role_not_supported(self.kind, "text-to-speech")
        yield  # pragma: no cover - never reached; keeps this an async generator function

    # --- errors ---

    def _map_error(self, response: httpx.Response) -> ProviderError:
        info = _error_info(error_body(response)) or _ErrorInfo(response.text, None, None)
        kind: type[ProviderError] | None = None
        if {info.code, info.type} & _CONTEXT_CODES or any(
            phrase in info.message.lower() for phrase in _CONTEXT_PHRASES
        ):
            kind = ContextTooLongError
        elif info.code == "insufficient_quota":
            # Arrives as a 429, but waiting will not add credit.
            kind = ProviderAuthError
        return error_for_status(
            response.status_code,
            info.message,
            provider=self.kind,
            headers=response.headers,
            provider_code=info.provider_code,
            kind=kind,
        )

    def _raise_reported_error(self, data: Any) -> None:
        """An error sent inside the stream, after the 200."""
        if isinstance(data, dict) and "choices" not in data:
            info = _error_info(data)
            if info is not None:
                raise stream_error(
                    info.message, provider=self.kind, provider_code=info.provider_code
                )


def _response_format(schema: dict[str, Any]) -> dict[str, Any]:
    """OpenAI's strict mode guarantees the shape but accepts only a subset of JSON Schema, and an
    object at the root. Outside it the schema is sent as a hint and checked by the gateway."""
    json_schema: dict[str, Any] = {"name": "response", "schema": schema}
    if schema.get("type") == "object" and strict_compatible(schema):
        json_schema["strict"] = True
    return {"type": "json_schema", "json_schema": json_schema}
