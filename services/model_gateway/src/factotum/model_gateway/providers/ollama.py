"""Ollama, through its native API.

The native API is used rather than Ollama's OpenAI-compatible one because it is the one that lets
the caller set the context window, and Ollama's default window is small enough to truncate a
prompt silently. Structured output uses ``format``, which constrains the decoding to the schema.
"""

from collections.abc import AsyncGenerator, Sequence
from typing import Any

import httpx
from pydantic import Field

from factotum.contracts.chat import (
    ChatRequest,
    ChatResult,
    EventUnion,
    FinishEvent,
    FinishReason,
    TextDelta,
    UsageEvent,
)
from factotum.contracts.errors import ContextTooLongError, ProviderError, ProviderUnavailableError
from factotum.contracts.models import ProviderKind
from factotum.model_gateway.providers.base import (
    EmbeddingBatch,
    ResolvedBinding,
    WireModel,
    api_url,
    bearer_headers,
    effective_max_output_tokens,
    effective_temperature,
    error_body,
    error_for_status,
    iter_ndjson,
    parse_wire,
    post_json,
    stream_error,
    stream_post,
    usage_of,
)
from factotum.model_gateway.structured import with_json_instructions

_ANSWER_ROOM = 2048
"""Room for the answer in the context window when the binding does not cap it."""

_FINISH_REASONS = {"stop": FinishReason.STOP, "length": FinishReason.LENGTH}


class _Message(WireModel):
    content: str = ""


class _Answer(WireModel):
    """A whole (non-streamed) answer."""

    message: _Message
    done_reason: str | None = None
    prompt_eval_count: int | None = None
    eval_count: int | None = None


class _Line(WireModel):
    """One line of a streamed answer; the last one has ``done`` and the counters."""

    message: _Message | None = None
    done: bool = False
    done_reason: str | None = None
    prompt_eval_count: int | None = None
    eval_count: int | None = None


class _Embeddings(WireModel):
    embeddings: list[list[float]] = Field(min_length=1)
    prompt_eval_count: int | None = None


def _finish_reason(raw: str | None) -> FinishReason:
    # Older servers send no reason with the last line: it stopped.
    return _FINISH_REASONS.get(raw or "stop", FinishReason.OTHER)


def _context_window(resolved: ResolvedBinding) -> int:
    """The window to ask for, prompt and answer together. It depends on the binding alone: Ollama
    reloads the model when the window changes, so a value that varied per request would."""
    options = resolved.options
    if options.num_ctx is not None:
        return options.num_ctx
    return options.max_input_tokens + (options.max_output_tokens or _ANSWER_ROOM)


class OllamaAdapter:
    """Ollama has no keys of its own: one is sent, as a bearer token, only when the binding has
    one, for a proxy in front of the server or for Ollama's hosted service."""

    kind = ProviderKind.OLLAMA

    def native_structured_output(self, resolved: ResolvedBinding) -> bool:
        declared = resolved.options.native_structured_output
        return True if declared is None else declared

    # --- chat ---

    async def chat(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, request: ChatRequest
    ) -> ChatResult:
        body = await post_json(
            client,
            api_url(resolved, "api/chat"),
            headers=bearer_headers(resolved),
            payload=self._chat_payload(resolved, request, stream=False),
            provider=self.kind,
            map_error=self._map_error,
        )
        self._raise_reported_error(body)
        answer = parse_wire(_Answer, body, self.kind)
        return ChatResult(
            text=answer.message.content,
            finish_reason=_finish_reason(answer.done_reason),
            usage=usage_of(answer.prompt_eval_count, answer.eval_count),
        )

    async def chat_stream(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, request: ChatRequest
    ) -> AsyncGenerator[EventUnion]:
        """Text as it arrives, then the usage, then the finish, last."""
        last: _Line | None = None
        async with stream_post(
            client,
            api_url(resolved, "api/chat"),
            headers=bearer_headers(resolved),
            payload=self._chat_payload(resolved, request, stream=True),
            provider=self.kind,
            map_error=self._map_error,
        ) as response:
            async for data in iter_ndjson(response, self.kind):
                self._raise_reported_error(data)
                line = parse_wire(_Line, data, self.kind)
                if line.message is not None and line.message.content:
                    yield TextDelta(text=line.message.content)
                if line.done:
                    last = line
                    break
        if last is None:
            raise ProviderUnavailableError(
                f"{self.kind.value}: the stream ended before the answer was complete",
                details={"provider": self.kind.value},
            )
        usage = usage_of(last.prompt_eval_count, last.eval_count)
        if usage is not None:
            yield UsageEvent(usage=usage)
        yield FinishEvent(reason=_finish_reason(last.done_reason))

    def _chat_payload(
        self, resolved: ResolvedBinding, request: ChatRequest, *, stream: bool
    ) -> dict[str, Any]:
        options: dict[str, Any] = {"num_ctx": _context_window(resolved)}
        max_tokens = effective_max_output_tokens(resolved, request)
        if max_tokens is not None:
            options["num_predict"] = max_tokens
        temperature = effective_temperature(resolved, request)
        if temperature is not None:
            options["temperature"] = temperature

        messages = request.messages
        payload: dict[str, Any] = {"model": resolved.model, "options": options, "stream": stream}
        if request.response_schema is not None:
            payload["format"] = request.response_schema
            # The constraint guarantees the shape, but the model does not know it is there: telling
            # it what is wanted gives answers that are right, not only well-formed.
            messages = with_json_instructions(messages, request.response_schema)
        payload["messages"] = [
            {"role": message.role.value, "content": message.content} for message in messages
        ]
        return payload

    # --- embeddings ---

    async def embed(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, inputs: Sequence[str]
    ) -> EmbeddingBatch:
        # An input that does not fit is an error, not a silently shortened text: a truncated chunk
        # embeds as something else, and nobody would notice.
        payload: dict[str, Any] = {
            "model": resolved.model,
            "input": list(inputs),
            "truncate": False,
        }
        if resolved.options.num_ctx is not None:
            payload["options"] = {"num_ctx": resolved.options.num_ctx}
        if resolved.options.dimensions is not None:
            payload["dimensions"] = resolved.options.dimensions
        body = await post_json(
            client,
            api_url(resolved, "api/embed"),
            headers=bearer_headers(resolved),
            payload=payload,
            provider=self.kind,
            map_error=self._map_error,
        )
        self._raise_reported_error(body)
        parsed = parse_wire(_Embeddings, body, self.kind)
        return EmbeddingBatch(
            vectors=tuple(tuple(vector) for vector in parsed.embeddings),
            usage=usage_of(parsed.prompt_eval_count, None),
        )

    # --- errors ---

    def _map_error(self, response: httpx.Response) -> ProviderError:
        message = _error_text(error_body(response)) or response.text
        kind = ContextTooLongError if "exceeds the context length" in message.lower() else None
        return error_for_status(
            response.status_code, message, provider=self.kind, headers=response.headers, kind=kind
        )

    def _raise_reported_error(self, data: Any) -> None:
        """An error sent in the body of an answer that has a good status."""
        message = _error_text(data)
        if message is not None:
            raise stream_error(message, provider=self.kind)


def _error_text(body: Any) -> str | None:
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, str):
            return error
    return None
