"""Anthropic, through the Messages API.

Where it differs from the others, the adapter absorbs the difference: system messages are lifted out
of the conversation, ``max_tokens`` (which the API requires) gets a default, and a temperature
above the 1.0 the API accepts is held at it. Anthropic offers no embeddings.
"""

from collections.abc import AsyncGenerator, Sequence
from typing import Any

import httpx

from factotum.contracts.audio import AudioPayload
from factotum.contracts.chat import (
    ChatRequest,
    ChatResult,
    EventUnion,
    FinishEvent,
    FinishReason,
    MessageRole,
    TextDelta,
    UsageEvent,
)
from factotum.contracts.errors import (
    ContextTooLongError,
    InvalidRequestError,
    ProviderAuthError,
    ProviderError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    RateLimitedError,
    RoleNotSupportedError,
)
from factotum.contracts.models import ProviderKind
from factotum.model_gateway.providers.base import (
    EmbeddingBatch,
    ResolvedBinding,
    SpeechPiece,
    WireModel,
    api_url,
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
from factotum.model_gateway.structured import json_instructions, strict_compatible

API_VERSION = "2023-06-01"

_DEFAULT_MAX_TOKENS = 4096
"""The API insists on a limit. This one is within what every current model can produce; a user who
wants longer answers raises it on the binding."""

_MAX_TEMPERATURE = 1.0

_STOP_REASONS = {
    "end_turn": FinishReason.STOP,
    "stop_sequence": FinishReason.STOP,
    "max_tokens": FinishReason.LENGTH,
    "model_context_window_exceeded": FinishReason.LENGTH,
    "refusal": FinishReason.CONTENT_FILTER,
}

# What an error event says, when the stream has no HTTP status to say it.
_ERROR_KINDS: dict[str, type[ProviderError]] = {
    "authentication_error": ProviderAuthError,
    "permission_error": ProviderAuthError,
    "invalid_request_error": InvalidRequestError,
    "not_found_error": InvalidRequestError,
    "request_too_large": ContextTooLongError,
    "rate_limit_error": RateLimitedError,
    "timeout_error": ProviderTimeoutError,
    "overloaded_error": ProviderUnavailableError,
    "api_error": ProviderUnavailableError,
}


class _Block(WireModel):
    type: str
    text: str | None = None


class _WireUsage(WireModel):
    input_tokens: int | None = None
    output_tokens: int | None = None


class _Answer(WireModel):
    content: list[_Block]
    stop_reason: str | None = None
    usage: _WireUsage | None = None


class _Delta(WireModel):
    type: str | None = None
    text: str | None = None
    stop_reason: str | None = None


class _Started(WireModel):
    usage: _WireUsage | None = None


class _ErrorBody(WireModel):
    type: str | None = None
    message: str | None = None


class _Event(WireModel):
    """The events of a stream, told apart by ``type``; each one uses only some of these fields."""

    type: str
    delta: _Delta | None = None
    message: _Started | None = None
    usage: _WireUsage | None = None
    error: _ErrorBody | None = None


class _ErrorEnvelope(WireModel):
    error: _ErrorBody


def _finish_reason(raw: str | None) -> FinishReason:
    return _STOP_REASONS.get(raw or "", FinishReason.OTHER)


def _known_case(message: str) -> type[ProviderError] | None:
    """The cases where the words say more than the status does: both arrive as a 400."""
    lowered = message.lower()
    if "prompt is too long" in lowered:
        return ContextTooLongError
    if "credit balance is too low" in lowered:
        # Waiting will not add credit.
        return ProviderAuthError
    return None


def _headers(resolved: ResolvedBinding) -> dict[str, str]:
    headers = {"anthropic-version": API_VERSION}
    if resolved.api_key is not None:
        headers["x-api-key"] = resolved.api_key.get_secret_value()
    return headers


class AnthropicAdapter:
    kind = ProviderKind.ANTHROPIC

    def native_structured_output(self, resolved: ResolvedBinding) -> bool:
        declared = resolved.options.native_structured_output
        return True if declared is None else declared

    # --- chat ---

    async def chat(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, request: ChatRequest
    ) -> ChatResult:
        body = await post_json(
            client,
            api_url(resolved, "v1/messages"),
            headers=_headers(resolved),
            payload=self._payload(resolved, request, stream=False),
            provider=self.kind,
            map_error=self._map_error,
        )
        answer = parse_wire(_Answer, body, self.kind)
        text = "".join(block.text or "" for block in answer.content if block.type == "text")
        usage = answer.usage
        return ChatResult(
            text=text,
            finish_reason=_finish_reason(answer.stop_reason),
            usage=usage_of(
                usage.input_tokens if usage else None, usage.output_tokens if usage else None
            ),
        )

    async def chat_stream(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, request: ChatRequest
    ) -> AsyncGenerator[EventUnion]:
        """Text as it arrives, then the usage, then the finish, last."""
        input_tokens: int | None = None
        output_tokens: int | None = None
        stop_reason: str | None = None
        finished = False
        async with stream_post(
            client,
            api_url(resolved, "v1/messages"),
            headers=_headers(resolved),
            payload=self._payload(resolved, request, stream=True),
            provider=self.kind,
            map_error=self._map_error,
        ) as response:
            async for message in iter_sse(response):
                event = parse_wire(_Event, parse_sse_json(message, self.kind), self.kind)
                match event.type:
                    case "message_start":
                        started = event.message.usage if event.message else None
                        if started is not None:
                            input_tokens = started.input_tokens
                            output_tokens = started.output_tokens
                    case "content_block_delta":
                        if event.delta and event.delta.type == "text_delta" and event.delta.text:
                            yield TextDelta(text=event.delta.text)
                    case "message_delta":
                        if event.delta and event.delta.stop_reason:
                            stop_reason = event.delta.stop_reason
                        if event.usage is not None:
                            # The counters here are running totals, not increments.
                            input_tokens = _latest(event.usage.input_tokens, input_tokens)
                            output_tokens = _latest(event.usage.output_tokens, output_tokens)
                    case "message_stop":
                        finished = True
                        break
                    case "error":
                        raise self._reported_error(event.error)
                    case _:
                        continue  # ping, content_block_start/stop, thinking...
        if not finished:
            raise ProviderUnavailableError(
                f"{self.kind.value}: the stream ended before the answer was complete",
                details={"provider": self.kind.value},
            )
        usage = usage_of(input_tokens, output_tokens)
        if usage is not None:
            yield UsageEvent(usage=usage)
        yield FinishEvent(reason=_finish_reason(stop_reason))

    def _payload(
        self, resolved: ResolvedBinding, request: ChatRequest, *, stream: bool
    ) -> dict[str, Any]:
        system = [m.content for m in request.messages if m.role is MessageRole.SYSTEM]
        schema = request.response_schema
        if schema is not None:
            # Told in the prompt whichever way it is enforced: a model does better at what it
            # knows it is asked.
            system.append(json_instructions(schema))

        payload: dict[str, Any] = {
            "model": resolved.model,
            "max_tokens": effective_max_output_tokens(resolved, request) or _DEFAULT_MAX_TOKENS,
            "messages": [
                {"role": m.role.value, "content": m.content}
                for m in request.messages
                if m.role is not MessageRole.SYSTEM
            ],
        }
        if system:
            payload["system"] = "\n\n".join(system)
        temperature = effective_temperature(resolved, request)
        if temperature is not None:
            payload["temperature"] = min(temperature, _MAX_TEMPERATURE)
        if stream:
            payload["stream"] = True
        if schema is not None and schema.get("type") == "object" and strict_compatible(schema):
            # Only a schema in the basic subset is handed to the API for constrained decoding;
            # any other is left to the prompt and to the check that follows.
            payload["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
        return payload

    # --- embeddings ---

    async def embed(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, inputs: Sequence[str]
    ) -> EmbeddingBatch:
        raise RoleNotSupportedError(
            "anthropic has no embeddings API: bind the embedding role to another provider",
            details={"provider": self.kind.value},
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
        envelope = self._error_envelope(error_body(response))
        message = (envelope.message if envelope else None) or response.text
        return error_for_status(
            response.status_code,
            message,
            provider=self.kind,
            headers=response.headers,
            provider_code=envelope.type if envelope else None,
            kind=_known_case(message),
        )

    @staticmethod
    def _error_envelope(body: Any) -> _ErrorBody | None:
        try:
            return _ErrorEnvelope.model_validate(body).error
        except ValueError:
            return None

    def _reported_error(self, error: _ErrorBody | None) -> ProviderError:
        """An error event: the status was 200, so the type says what happened."""
        message = (error.message if error else None) or ""
        kind = _known_case(message) or _ERROR_KINDS.get((error.type if error else None) or "")
        return stream_error(
            message,
            provider=self.kind,
            kind=kind or ProviderUnavailableError,
            provider_code=error.type if error else None,
        )


def _latest(new: int | None, old: int | None) -> int | None:
    return old if new is None else new
