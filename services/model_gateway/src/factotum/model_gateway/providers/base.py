"""What every provider adapter shares: the resolved binding, the interface and the plumbing.

The plumbing turns what providers do differently into what callers can rely on: HTTP statuses and
transport failures become typed errors, and the two streaming formats (server-sent events and
newline-delimited JSON) become iterators. Nothing here knows a particular provider.
"""

import email.utils
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from factotum.common.redaction import redact_text
from factotum.contracts.audio import AudioPayload
from factotum.contracts.chat import ChatRequest, ChatResult, EventUnion, Usage
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
from factotum.contracts.models import BindingOptions, ModelBinding, ProviderKind

_MAX_MESSAGE_CHARS = 500
_MAX_RETRY_AFTER_S = 3600.0


@dataclass(frozen=True, slots=True)
class ResolvedBinding:
    """A binding with everything needed to call the provider.

    The address has its default applied (it is empty for a provider that runs in the gateway's own
    process, which is never called over a network) and the key is in memory (``SecretStr`` keeps it
    out of ``repr``). It is built for one call and never leaves the gateway.
    """

    binding: ModelBinding
    base_url: str
    api_key: SecretStr | None

    @property
    def model(self) -> str:
        return self.binding.model

    @property
    def options(self) -> BindingOptions:
        return self.binding.options


@dataclass(frozen=True, slots=True)
class EmbeddingBatch:
    vectors: tuple[tuple[float, ...], ...]
    usage: Usage | None = None


@dataclass(frozen=True, slots=True)
class SpeechPiece:
    """What a voice has made so far, as it makes it: samples of ``StreamFormat.PCM_S16LE`` (16-bit
    little-endian, one channel) at ``sample_rate``.

    A voice makes its pieces as big as suits it, Piper's a sentence each, and where one ends is not
    where a sample does when a provider hands over what a network gave it: the gateway puts the
    pieces together and cuts them into chunks of whole samples (``ModelGateway.synthesize_stream``)
    before anyone else sees them.
    """

    sample_rate: int
    pcm: bytes


def api_url(resolved: ResolvedBinding, path: str) -> str:
    """The address of an endpoint under the binding's base URL."""
    return f"{resolved.base_url}/{path}"


def bearer_headers(resolved: ResolvedBinding) -> dict[str, str]:
    """``Authorization: Bearer`` when the binding has a key, no headers otherwise. The key goes
    in a header and nowhere else: URLs end up in logs."""
    if resolved.api_key is None:
        return {}
    return {"authorization": f"Bearer {resolved.api_key.get_secret_value()}"}


def effective_max_output_tokens(resolved: ResolvedBinding, request: ChatRequest) -> int | None:
    """The output limit to send: the caller's, held under the cap the user set on the binding."""
    limits = [
        limit
        for limit in (request.max_output_tokens, resolved.options.max_output_tokens)
        if limit is not None
    ]
    return min(limits) if limits else None


def effective_temperature(resolved: ResolvedBinding, request: ChatRequest) -> float | None:
    """The caller's temperature, or else the one the user set on the binding, or else none: the
    provider's own default is the safest choice (some models accept no other)."""
    if request.temperature is not None:
        return request.temperature
    return resolved.options.temperature


def usage_of(input_tokens: int | None, output_tokens: int | None) -> Usage | None:
    """Token usage, or None when the provider reported nothing."""
    if input_tokens is None and output_tokens is None:
        return None
    return Usage(input_tokens=input_tokens, output_tokens=output_tokens)


class ProviderAdapter(Protocol):
    """What the gateway asks of a provider: chat, embeddings, and speech. One adapter answers for
    one ``ProviderKind``; a provider that offers none of these for a given call says so with
    ``RoleNotSupportedError`` (``role_not_supported``, below) rather than pretending."""

    kind: ProviderKind

    def native_structured_output(self, resolved: ResolvedBinding) -> bool:
        """Whether the provider can constrain the output to a JSON schema (for this binding)."""
        ...

    async def chat(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, request: ChatRequest
    ) -> ChatResult: ...

    def chat_stream(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, request: ChatRequest
    ) -> AsyncGenerator[EventUnion]:
        """Text as it arrives, then the usage if the provider reports it, then the finish, last."""
        ...

    async def embed(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, inputs: Sequence[str]
    ) -> EmbeddingBatch: ...

    async def transcribe(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, audio: AudioPayload
    ) -> str:
        """What was said in ``audio``."""
        ...

    async def synthesize(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, text: str
    ) -> AudioPayload:
        """``text``, spoken aloud."""
        ...

    def synthesize_stream(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, text: str
    ) -> AsyncGenerator[SpeechPiece]:
        """``text``, spoken aloud, as the voice makes it. A failure to begin is raised when the
        first piece is asked for, so that it can be retried before anything has been sent; one
        after that ends the stream. A provider that makes the whole clip at once yields it as one
        piece."""
        ...


def role_not_supported(provider: ProviderKind, what: str) -> RoleNotSupportedError:
    """The provider does not offer ``what`` (``"embeddings"``, ``"speech-to-text"``...) at all:
    the role must be bound to another provider instead."""
    return RoleNotSupportedError(
        f"{provider.value} has no {what} API: bind the role to another provider",
        details={"provider": provider.value},
    )


# --- errors ---


def retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    """How long the provider asked us to wait, from a ``Retry-After`` header (seconds or a date)."""
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        seconds = (when.astimezone(UTC) - datetime.now(UTC)).total_seconds()
    return max(0.0, min(seconds, _MAX_RETRY_AFTER_S))


def error_for_status(
    status: int,
    message: str,
    *,
    provider: ProviderKind,
    headers: Mapping[str, str] | None = None,
    provider_code: str | None = None,
    kind: type[ProviderError] | None = None,
    retry_after_s: float | None = None,
) -> ProviderError:
    """The typed error for an HTTP status. The message is redacted and cut: providers sometimes
    quote the key they were given.

    The status decides the kind unless the adapter knows better from what the provider said (an
    exhausted quota arrives as a 429 and will not pass by waiting): then it passes ``kind``. A
    provider that says how long to wait somewhere other than the ``Retry-After`` header passes it
    as ``retry_after_s``.
    """
    label = _label(provider, message, f"HTTP {status}")
    details: dict[str, Any] = {"provider": provider.value, "status": status}
    if provider_code:
        details["provider_code"] = provider_code
    if kind is not None:
        return kind(label, details=details)
    match status:
        case 401 | 403:
            return ProviderAuthError(label, details=details)
        case 408:
            return ProviderTimeoutError(label, details=details)
        case 413:
            return ContextTooLongError(label, details=details)
        case 429:
            if retry_after_s is None:
                retry_after_s = retry_after_seconds(headers or {})
            return RateLimitedError(label, retry_after_s=retry_after_s, details=details)
        case 400 | 404 | 409 | 422:
            return InvalidRequestError(label, details=details)
        case _ if status >= 500:
            return ProviderUnavailableError(label, details=details)
        case _:
            return ProviderError(label, details=details)


def _label(provider: ProviderKind, message: str, fallback: str) -> str:
    text = redact_text(message).strip()[:_MAX_MESSAGE_CHARS] or fallback
    return f"{provider.value}: {text}"


def stream_error(
    message: str,
    *,
    provider: ProviderKind,
    kind: type[ProviderError] = ProviderUnavailableError,
    provider_code: str | None = None,
) -> ProviderError:
    """The error for a failure a provider reports inside a stream, when the HTTP status is already
    200 and there is no status to go by."""
    details: dict[str, Any] = {"provider": provider.value}
    if provider_code:
        details["provider_code"] = provider_code
    return kind(_label(provider, message, "the stream reported an error"), details=details)


class WireModel(BaseModel):
    """Base of the models that describe what a provider sends. They read only what the gateway
    uses and ignore the rest, so a field a provider adds does not break the adapter."""

    model_config = ConfigDict(extra="ignore")


def parse_wire[W: WireModel](model: type[W], data: Any, provider: ProviderKind) -> W:
    """Read a provider's answer into ``model``; an answer of another shape is a provider failure.
    The data is not repeated in the error: it is the model's output."""
    try:
        return model.model_validate(data)
    except ValidationError:
        raise ProviderUnavailableError(
            f"{provider.value}: the answer did not have the expected shape",
            details={"provider": provider.value},
        ) from None


def parse_json(response: httpx.Response, provider: ProviderKind) -> Any:
    """The JSON body of a successful response; anything else is a provider failure."""
    try:
        return response.json()
    except ValueError:
        raise ProviderUnavailableError(
            f"{provider.value}: the answer was not valid JSON",
            details={"provider": provider.value, "status": response.status_code},
        ) from None


def error_body(response: httpx.Response) -> Any:
    """The parsed body of an error response, or None when it is not JSON."""
    try:
        return response.json()
    except ValueError:
        return None


@contextmanager
def transport_errors(provider: ProviderKind) -> Iterator[None]:
    """Turn what httpx raises into typed errors. Only the exception type is reported: the message
    of a transport error may quote the address."""
    try:
        yield
    except httpx.TimeoutException:
        raise ProviderTimeoutError(
            f"{provider.value}: no answer in time", details={"provider": provider.value}
        ) from None
    except httpx.TransportError as error:
        raise ProviderUnavailableError(
            f"{provider.value}: could not be reached ({type(error).__name__})",
            details={"provider": provider.value},
        ) from None


ErrorMapper = Callable[[httpx.Response], ProviderError]


async def post_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
    provider: ProviderKind,
    map_error: ErrorMapper,
) -> Any:
    """POST a JSON body and return the parsed JSON answer."""
    with transport_errors(provider):
        response = await client.post(url, headers=headers, json=payload)
    if response.status_code >= 400:
        raise map_error(response)
    return parse_json(response, provider)


@asynccontextmanager
async def stream_post(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
    provider: ProviderKind,
    map_error: ErrorMapper,
) -> AsyncIterator[httpx.Response]:
    """POST a JSON body and stream the answer. An error status raises before anything is read;
    a transport failure in the middle of the stream is mapped as well."""
    with transport_errors(provider):
        async with client.stream("POST", url, headers=headers, json=payload) as response:
            if response.status_code >= 400:
                await response.aread()
                raise map_error(response)
            yield response


# --- streaming formats ---


@dataclass(frozen=True, slots=True)
class SseMessage:
    event: str | None
    data: str


async def iter_sse(response: httpx.Response) -> AsyncIterator[SseMessage]:
    """Server-sent events: messages separated by a blank line, with ``event:`` and ``data:`` fields
    and ``:`` comments (keep-alives) that are ignored."""
    event: str | None = None
    data: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if data:
                yield SseMessage(event, "\n".join(data))
            event, data = None, []
        elif line.startswith(":"):
            continue
        else:
            field, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if field == "event":
                event = value
            elif field == "data":
                data.append(value)
    if data:
        yield SseMessage(event, "\n".join(data))


async def iter_ndjson(response: httpx.Response, provider: ProviderKind) -> AsyncIterator[Any]:
    """Newline-delimited JSON: one JSON value per line."""
    async for line in response.aiter_lines():
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except ValueError:
            raise ProviderUnavailableError(
                f"{provider.value}: the stream contained something that is not JSON",
                details={"provider": provider.value},
            ) from None


def parse_sse_json(message: SseMessage, provider: ProviderKind) -> Any:
    """The JSON in the data of an event."""
    try:
        return json.loads(message.data)
    except ValueError:
        raise ProviderUnavailableError(
            f"{provider.value}: the stream contained something that is not JSON",
            details={"provider": provider.value},
        ) from None
