"""The model gateway: from a role and a request to a provider's answer.

It is the only place that turns "the chat model of this user" into a provider, an address and a
key. Before the network it decides, in a fixed order, which binding serves the request, which key
goes with it and whether the data may go there at all. Then it makes the call, retried where
retrying may help, and returns the answer in the normalized shape.
"""

import asyncio
import logging
import random
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from dataclasses import dataclass

import httpx

from factotum.common.urlguard import UrlPolicy, guarded_client
from factotum.contracts.chat import (
    ChatRequest,
    ChatResult,
    ErrorEvent,
    EventUnion,
    Usage,
    UsageEvent,
)
from factotum.contracts.embeddings import EmbeddingRequest, EmbeddingResult, EmbeddingSpace
from factotum.contracts.errors import (
    FactotumError,
    InvalidRequestError,
    PolicyViolationError,
    ProviderError,
    ProviderUnavailableError,
)
from factotum.contracts.identity import Principal
from factotum.contracts.models import (
    HOSTED_PROVIDERS,
    DataClassification,
    Locality,
    ModelBinding,
    ModelRole,
    Profile,
    ProviderKind,
    StructuredOutputSupport,
)
from factotum.model_gateway.bindings import BindingRepository, select_binding
from factotum.model_gateway.locality import infer_locality
from factotum.model_gateway.providers.anthropic import AnthropicAdapter
from factotum.model_gateway.providers.base import EmbeddingBatch, ProviderAdapter, ResolvedBinding
from factotum.model_gateway.providers.gemini import GeminiAdapter
from factotum.model_gateway.providers.ollama import OllamaAdapter
from factotum.model_gateway.providers.openai import OpenAIAdapter
from factotum.model_gateway.retry import RetryPolicy, with_retries
from factotum.model_gateway.secrets import SecretResolver
from factotum.model_gateway.structured import run_structured

logger = logging.getLogger("factotum.model_gateway")

DEFAULT_BASE_URLS: Mapping[ProviderKind, str] = {
    ProviderKind.OPENAI: "https://api.openai.com/v1",
    ProviderKind.ANTHROPIC: "https://api.anthropic.com",
    ProviderKind.GEMINI: "https://generativelanguage.googleapis.com",
}
"""Where the hosted providers are. The others have no fixed address: the user gives it."""

_CONNECT_TIMEOUT_S = 10.0

ClientFactory = Callable[[ResolvedBinding], httpx.AsyncClient]


def default_adapters() -> dict[ProviderKind, ProviderAdapter]:
    return {
        ProviderKind.OPENAI: OpenAIAdapter(ProviderKind.OPENAI),
        ProviderKind.OPENAI_COMPATIBLE: OpenAIAdapter(ProviderKind.OPENAI_COMPATIBLE),
        ProviderKind.OLLAMA: OllamaAdapter(),
        ProviderKind.ANTHROPIC: AnthropicAdapter(),
        ProviderKind.GEMINI: GeminiAdapter(),
    }


def guarded_client_factory(resolved: ResolvedBinding) -> httpx.AsyncClient:
    """A client that reaches only what the locality of the binding allows: a local model may be on
    the private network, a remote one only on the public internet. Every connection is vetted."""
    policy = UrlPolicy.local() if resolved.locality is Locality.LOCAL else UrlPolicy.remote()
    timeout = resolved.options.timeout_s
    return guarded_client(
        policy, timeout=httpx.Timeout(timeout, connect=min(_CONNECT_TIMEOUT_S, timeout))
    )


@dataclass(frozen=True, slots=True)
class _Call:
    role: ModelRole
    principal: Principal
    resolved: ResolvedBinding
    adapter: ProviderAdapter
    client: httpx.AsyncClient


class ModelGateway:
    def __init__(
        self,
        repository: BindingRepository,
        secrets: SecretResolver,
        *,
        adapters: Mapping[ProviderKind, ProviderAdapter] | None = None,
        client_factory: ClientFactory = guarded_client_factory,
        retry: RetryPolicy = RetryPolicy(),  # noqa: B008 - a frozen value, safe to share
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._repository = repository
        self._secrets = secrets
        self._adapters = dict(adapters) if adapters is not None else default_adapters()
        self._client_factory = client_factory
        self._retry = retry
        self._sleep = sleep
        self._jitter = jitter

    # --- the operations ---

    async def chat(self, principal: Principal, request: ChatRequest) -> ChatResult:
        """The answer to ``request``; with a ``response_schema``, a validated JSON value in
        ``parsed``. A failure raises, typed."""
        started = time.monotonic()
        call: _Call | None = None
        usage: Usage | None = None
        outcome = "ok"
        try:
            call = prepared = await self._prepare(
                principal, request.role, request.data_classification
            )
            async with prepared.client:
                result = await self._chat(prepared, request)
            usage = result.usage
            return result
        except FactotumError as error:
            outcome = error.code
            raise
        finally:
            self._log(call, request.role, principal, outcome, started, usage)

    async def chat_stream(
        self, principal: Principal, request: ChatRequest
    ) -> AsyncGenerator[EventUnion]:
        """The answer as it is produced: text, then the usage if it is known, then the finish.

        A failure before the first event raises, typed, so the caller can answer with the right
        status. One after it ends the stream with an ``ErrorEvent``: the caller has already been
        told the answer is coming.
        """
        started = time.monotonic()
        call: _Call | None = None
        usage: Usage | None = None
        outcome = "cancelled"
        try:
            call = prepared = await self._prepare(
                principal, request.role, request.data_classification
            )
            async with prepared.client:
                stream = await self._retrying(
                    lambda: _prime(
                        prepared.adapter.chat_stream(prepared.client, prepared.resolved, request)
                    )
                )
                try:
                    async for event in stream:
                        if isinstance(event, UsageEvent):
                            usage = event.usage
                        yield event
                    outcome = "ok"
                except ProviderError as error:
                    outcome = error.code
                    yield ErrorEvent(code=error.code, message=error.message)
                finally:
                    await stream.aclose()
        except FactotumError as error:
            outcome = error.code
            raise
        finally:
            self._log(call, request.role, principal, outcome, started, usage)

    async def embed(self, principal: Principal, request: EmbeddingRequest) -> EmbeddingResult:
        """One vector per input, and the id of the space the vectors live in."""
        started = time.monotonic()
        call: _Call | None = None
        usage: Usage | None = None
        outcome = "ok"
        try:
            call = prepared = await self._prepare(
                principal, ModelRole.EMBEDDING, request.data_classification
            )
            async with prepared.client:
                batch = await self._retrying(
                    lambda: prepared.adapter.embed(
                        prepared.client, prepared.resolved, request.inputs
                    )
                )
            result = self._embedding_result(prepared, request, batch)
            usage = result.usage
            return result
        except FactotumError as error:
            outcome = error.code
            raise
        finally:
            self._log(call, ModelRole.EMBEDDING, principal, outcome, started, usage)

    async def profile(self, principal: Principal, role: ModelRole) -> Profile:
        """What ``role`` can do for this principal, without saying which model does it."""
        binding = await select_binding(
            self._repository, principal.tenant_id, principal.acts_as, role
        )
        resolved = self._resolve(binding, with_secret=False)
        structured: StructuredOutputSupport | None = None
        if role.is_chat:
            native = self._adapters[binding.provider].native_structured_output(resolved)
            structured = (
                StructuredOutputSupport.NATIVE if native else StructuredOutputSupport.EMULATED
            )
        return Profile(
            role=role,
            max_input_tokens=binding.options.max_input_tokens,
            max_output_tokens=binding.options.max_output_tokens,
            streaming=role.is_chat,
            structured_output=structured,
            locality=resolved.locality,
        )

    # --- what happens before the network ---

    async def _prepare(
        self, principal: Principal, role: ModelRole, classification: DataClassification
    ) -> _Call:
        binding = await select_binding(
            self._repository, principal.tenant_id, principal.acts_as, role
        )
        resolved = self._resolve(binding)
        if (
            classification is DataClassification.LOCAL_ONLY
            and resolved.locality is not Locality.LOCAL
        ):
            raise PolicyViolationError(
                f"the data is local-only and the model bound to {role.value} is not local",
                details={"role": role.value, "locality": resolved.locality.value},
            )
        return _Call(
            role=role,
            principal=principal,
            resolved=resolved,
            adapter=self._adapters[binding.provider],
            client=self._client_factory(resolved),
        )

    def _resolve(self, binding: ModelBinding, *, with_secret: bool = True) -> ResolvedBinding:
        base_url = binding.base_url or DEFAULT_BASE_URLS[binding.provider]
        locality = binding.locality
        if locality is None:
            # A hosted provider is remote whatever its address says: a proxy on the LAN in front
            # of OpenAI is still OpenAI. Only a server the user pointed us at is judged by where
            # it is.
            locality = (
                Locality.REMOTE
                if binding.provider in HOSTED_PROVIDERS
                else infer_locality(base_url)
            )
        return ResolvedBinding(
            binding=binding,
            base_url=base_url,
            api_key=self._secrets.resolve(binding.secret_ref) if with_secret else None,
            locality=locality,
        )

    # --- the calls ---

    async def _retrying[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        return await with_retries(operation, self._retry, sleep=self._sleep, jitter=self._jitter)

    async def _chat(self, call: _Call, request: ChatRequest) -> ChatResult:
        async def once(sent: ChatRequest) -> ChatResult:
            return await self._retrying(lambda: call.adapter.chat(call.client, call.resolved, sent))

        if request.response_schema is None:
            return await once(request)
        native = call.adapter.native_structured_output(call.resolved)
        return await run_structured(once, request, native=native)

    @staticmethod
    def _embedding_result(
        call: _Call, request: EmbeddingRequest, batch: EmbeddingBatch
    ) -> EmbeddingResult:
        provider = call.resolved.binding.provider
        details = {"provider": provider.value}
        vectors = batch.vectors
        if len(vectors) != len(request.inputs):
            raise ProviderUnavailableError(
                f"{provider.value}: returned {len(vectors)} vectors "
                f"for {len(request.inputs)} inputs",
                details=details,
            )
        sizes = {len(vector) for vector in vectors}
        if len(sizes) != 1 or 0 in sizes:
            raise ProviderUnavailableError(
                f"{provider.value}: returned vectors of different sizes, or empty ones",
                details=details,
            )
        dimensions = sizes.pop()
        declared = call.resolved.options.dimensions
        if declared is not None and declared != dimensions:
            raise InvalidRequestError(
                f"{provider.value}: the binding declares {declared} dimensions "
                f"but the model returned {dimensions}",
                details=details,
            )
        space = EmbeddingSpace(provider=provider, model=call.resolved.model, dimensions=dimensions)
        return EmbeddingResult(
            vectors=vectors, dimensions=dimensions, space_id=space.id, usage=batch.usage
        )

    # --- observability ---

    @staticmethod
    def _log(
        call: _Call | None,
        role: ModelRole,
        principal: Principal,
        outcome: str,
        started: float,
        usage: Usage | None,
    ) -> None:
        """One line per call: what was asked of whom, how it ended, how long and how much. Never
        the content of the conversation."""
        fields: dict[str, object] = {
            "role": role.value,
            "tenant_id": str(principal.tenant_id),
            "outcome": outcome,
            "duration_ms": round((time.monotonic() - started) * 1000),
        }
        if call is not None:
            fields["provider"] = call.resolved.binding.provider.value
            fields["model"] = call.resolved.model
            fields["locality"] = call.resolved.locality.value
        if usage is not None:
            fields["input_tokens"] = usage.input_tokens
            fields["output_tokens"] = usage.output_tokens
        level = logging.INFO if outcome in {"ok", "cancelled"} else logging.WARNING
        logger.log(level, "model call", extra=fields)


async def _replay(
    first: EventUnion | None, rest: AsyncGenerator[EventUnion]
) -> AsyncGenerator[EventUnion]:
    try:
        if first is not None:
            yield first
        async for event in rest:
            yield event
    finally:
        await rest.aclose()


async def _prime(stream: AsyncGenerator[EventUnion]) -> AsyncGenerator[EventUnion]:
    """Wait for the first event, so that a failure to start is seen (and retried) before anything
    is delivered, and hand back a stream that replays it."""
    try:
        first = await anext(stream)
    except StopAsyncIteration:
        return _replay(None, stream)
    except BaseException:
        await stream.aclose()
        raise
    return _replay(first, stream)
