"""Google Gemini, through the Generative Language API.

Gemini's differences are absorbed here: the key travels in a header rather than in the URL the
official examples show (URLs end up in logs), roles are ``user`` and ``model``, system messages
become the system instruction, and a request the API refuses to answer is reported as a refusal,
not as an empty answer.
"""

import re
from collections.abc import AsyncGenerator, Sequence
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
    MessageRole,
    TextDelta,
    Usage,
    UsageEvent,
)
from factotum.contracts.errors import (
    ContentFilteredError,
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
    effective_max_output_tokens,
    effective_temperature,
    error_body,
    error_for_status,
    iter_sse,
    parse_sse_json,
    parse_wire,
    post_json,
    role_not_supported,
    stream_post,
    usage_of,
)
from factotum.model_gateway.structured import json_instructions, keywords_within

API_VERSION = "v1beta"

_EMBED_BATCH = 100
"""The most requests the API takes in one batch."""

# What the API documents for ``responseJsonSchema``. A schema using anything else goes in the
# prompt instead of being sent to be refused.
SCHEMA_KEYWORDS = frozenset(
    {
        "$id",
        "$defs",
        "$ref",
        "$anchor",
        "type",
        "format",
        "title",
        "description",
        "enum",
        "items",
        "prefixItems",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "anyOf",
        "oneOf",
        "properties",
        "additionalProperties",
        "required",
        "propertyOrdering",
    }
)

_FILTERED = frozenset(
    {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY"}
)
_RETRY_DELAY = re.compile(r"^(\d+(?:\.\d+)?)s$")


class _Part(WireModel):
    text: str | None = None
    thought: bool | None = None


class _Content(WireModel):
    parts: list[_Part] = Field(default_factory=list)


class _Candidate(WireModel):
    content: _Content | None = None
    finish_reason: str | None = Field(default=None, alias="finishReason")


class _WireUsage(WireModel):
    prompt_tokens: int | None = Field(default=None, alias="promptTokenCount")
    candidates_tokens: int | None = Field(default=None, alias="candidatesTokenCount")
    thoughts_tokens: int | None = Field(default=None, alias="thoughtsTokenCount")


class _Feedback(WireModel):
    block_reason: str | None = Field(default=None, alias="blockReason")


class _Response(WireModel):
    """An answer, or one chunk of a streamed answer."""

    candidates: list[_Candidate] = Field(default_factory=list)
    usage: _WireUsage | None = Field(default=None, alias="usageMetadata")
    feedback: _Feedback | None = Field(default=None, alias="promptFeedback")


class _Vector(WireModel):
    values: list[float]


class _Embedded(WireModel):
    embeddings: list[_Vector] = Field(min_length=1)


class _ErrorDetail(WireModel):
    type: str | None = Field(default=None, alias="@type")
    retry_delay: str | None = Field(default=None, alias="retryDelay")


class _ErrorBody(WireModel):
    code: int | None = None
    message: str | None = None
    status: str | None = None
    details: list[_ErrorDetail] = Field(default_factory=list)


class _ErrorEnvelope(WireModel):
    error: _ErrorBody


def _finish_reason(raw: str | None) -> FinishReason:
    if raw == "STOP":
        return FinishReason.STOP
    if raw == "MAX_TOKENS":
        return FinishReason.LENGTH
    if raw in _FILTERED:
        return FinishReason.CONTENT_FILTER
    return FinishReason.OTHER


def _text_of(candidate: _Candidate) -> str:
    """The words of the answer. Thought summaries are parts too, marked as such, and are not."""
    if candidate.content is None:
        return ""
    return "".join(part.text or "" for part in candidate.content.parts if not part.thought)


def _usage(wire: _WireUsage | None) -> Usage | None:
    if wire is None:
        return None
    if wire.candidates_tokens is None and wire.thoughts_tokens is None:
        output = None
    else:
        # What the model generated, thinking included: it is what is billed and what counts
        # against the output limit.
        output = (wire.candidates_tokens or 0) + (wire.thoughts_tokens or 0)
    return usage_of(wire.prompt_tokens, output)


def _headers(resolved: ResolvedBinding) -> dict[str, str]:
    if resolved.api_key is None:
        return {}
    return {"x-goog-api-key": resolved.api_key.get_secret_value()}


def _model_path(resolved: ResolvedBinding) -> str:
    model = resolved.model
    return model if "/" in model else f"models/{model}"


def _endpoint(resolved: ResolvedBinding, method: str) -> str:
    return api_url(resolved, f"{API_VERSION}/{_model_path(resolved)}:{method}")


class GeminiAdapter:
    kind = ProviderKind.GEMINI

    def native_structured_output(self, resolved: ResolvedBinding) -> bool:
        declared = resolved.options.native_structured_output
        return True if declared is None else declared

    # --- chat ---

    async def chat(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, request: ChatRequest
    ) -> ChatResult:
        body = await post_json(
            client,
            _endpoint(resolved, "generateContent"),
            headers=_headers(resolved),
            payload=self._payload(resolved, request),
            provider=self.kind,
            map_error=self._map_error,
        )
        response = parse_wire(_Response, body, self.kind)
        self._refuse_a_blocked_prompt(response)
        if not response.candidates:
            raise ProviderUnavailableError(
                f"{self.kind.value}: the answer had no candidates",
                details={"provider": self.kind.value},
            )
        candidate = response.candidates[0]
        return ChatResult(
            text=_text_of(candidate),
            finish_reason=_finish_reason(candidate.finish_reason),
            usage=_usage(response.usage),
        )

    async def chat_stream(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, request: ChatRequest
    ) -> AsyncGenerator[EventUnion]:
        """Text as it arrives, then the usage, then the finish, last."""
        finish: str | None = None
        usage: Usage | None = None
        async with stream_post(
            client,
            f"{_endpoint(resolved, 'streamGenerateContent')}?alt=sse",
            headers=_headers(resolved),
            payload=self._payload(resolved, request),
            provider=self.kind,
            map_error=self._map_error,
        ) as response:
            async for message in iter_sse(response):
                data = parse_sse_json(message, self.kind)
                self._raise_reported_error(data)
                chunk = parse_wire(_Response, data, self.kind)
                self._refuse_a_blocked_prompt(chunk)
                usage = _usage(chunk.usage) or usage
                if not chunk.candidates:
                    continue
                candidate = chunk.candidates[0]
                text = _text_of(candidate)
                if text:
                    yield TextDelta(text=text)
                finish = candidate.finish_reason or finish
        if finish is None:
            raise ProviderUnavailableError(
                f"{self.kind.value}: the stream ended before the answer was complete",
                details={"provider": self.kind.value},
            )
        if usage is not None:
            yield UsageEvent(usage=usage)
        yield FinishEvent(reason=_finish_reason(finish))

    def _payload(self, resolved: ResolvedBinding, request: ChatRequest) -> dict[str, Any]:
        system = [m.content for m in request.messages if m.role is MessageRole.SYSTEM]
        config: dict[str, Any] = {}
        max_tokens = effective_max_output_tokens(resolved, request)
        if max_tokens is not None:
            config["maxOutputTokens"] = max_tokens
        temperature = effective_temperature(resolved, request)
        if temperature is not None:
            config["temperature"] = temperature
        schema = request.response_schema
        if schema is not None:
            # Told in the prompt whichever way it is enforced: a model does better at what it
            # knows it is asked.
            system.append(json_instructions(schema))
            if keywords_within(schema, SCHEMA_KEYWORDS):
                config["responseMimeType"] = "application/json"
                config["responseJsonSchema"] = schema

        payload: dict[str, Any] = {
            "contents": [
                {
                    "role": "user" if m.role is MessageRole.USER else "model",
                    "parts": [{"text": m.content}],
                }
                for m in request.messages
                if m.role is not MessageRole.SYSTEM
            ]
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": text} for text in system]}
        if config:
            payload["generationConfig"] = config
        return payload

    def _refuse_a_blocked_prompt(self, response: _Response) -> None:
        """A prompt the API will not answer comes back without candidates and says why."""
        if response.candidates or response.feedback is None or not response.feedback.block_reason:
            return
        reason = response.feedback.block_reason
        raise ContentFilteredError(
            f"{self.kind.value}: the prompt was blocked ({reason})",
            details={"provider": self.kind.value, "block_reason": reason},
        )

    # --- embeddings ---

    async def embed(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, inputs: Sequence[str]
    ) -> EmbeddingBatch:
        model = _model_path(resolved)
        vectors: list[tuple[float, ...]] = []
        for start in range(0, len(inputs), _EMBED_BATCH):
            requests: list[dict[str, Any]] = []
            for text in inputs[start : start + _EMBED_BATCH]:
                item: dict[str, Any] = {"model": model, "content": {"parts": [{"text": text}]}}
                if resolved.options.dimensions is not None:
                    item["outputDimensionality"] = resolved.options.dimensions
                requests.append(item)
            body = await post_json(
                client,
                _endpoint(resolved, "batchEmbedContents"),
                headers=_headers(resolved),
                payload={"requests": requests},
                provider=self.kind,
                map_error=self._map_error,
            )
            parsed = parse_wire(_Embedded, body, self.kind)
            vectors.extend(tuple(vector.values) for vector in parsed.embeddings)
        return EmbeddingBatch(vectors=tuple(vectors))

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
        try:
            error = _ErrorEnvelope.model_validate(error_body(response)).error
        except ValueError:
            error = None
        return self._error_for(response.status_code, error, response.text, headers=response.headers)

    def _raise_reported_error(self, data: Any) -> None:
        """An error sent as a chunk of the stream: its own code says what it is."""
        try:
            error = _ErrorEnvelope.model_validate(data).error
        except ValueError:
            return
        raise self._error_for(error.code or 500, error, "")

    def _error_for(
        self,
        status: int,
        error: _ErrorBody | None,
        fallback: str,
        *,
        headers: httpx.Headers | None = None,
    ) -> ProviderError:
        message = (error.message if error else None) or fallback
        lowered = message.lower()
        kind: type[ProviderError] | None = None
        if "api key not valid" in lowered or "api key expired" in lowered:
            # Reported as a 400 (an invalid argument), but it is the key.
            kind = ProviderAuthError
        elif "input token count" in lowered and "exceeds the maximum" in lowered:
            kind = ContextTooLongError
        return error_for_status(
            status,
            message,
            provider=self.kind,
            headers=headers,
            provider_code=error.status if error else None,
            kind=kind,
            retry_after_s=_retry_delay(error) if error else None,
        )


def _retry_delay(error: _ErrorBody) -> float | None:
    """How long the API asked to wait, from the ``RetryInfo`` it attaches to a rate limit."""
    for detail in error.details:
        if detail.retry_delay:
            found = _RETRY_DELAY.match(detail.retry_delay)
            if found:
                return float(found.group(1))
    return None
