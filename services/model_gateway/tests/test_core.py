import logging
from typing import Any

import httpx
import pytest

from factotum.common.urlguard import UrlPolicy
from factotum.contracts.chat import (
    ChatResult,
    ErrorEvent,
    EventUnion,
    FinishEvent,
    FinishReason,
    TextDelta,
    Usage,
    UsageEvent,
)
from factotum.contracts.embeddings import EmbeddingRequest, EmbeddingSpace
from factotum.contracts.errors import (
    BindingNotFoundError,
    ContentFilteredError,
    InvalidRequestError,
    InvalidStructuredOutputError,
    ProviderAuthError,
    ProviderUnavailableError,
    RateLimitedError,
    RoleNotSupportedError,
    SecretNotFoundError,
    UrlNotAllowedError,
)
from factotum.contracts.identity import Owner, Subject
from factotum.contracts.models import (
    BindingOptions,
    ModelBinding,
    ModelRole,
    ProviderKind,
    StructuredOutputSupport,
)
from factotum.contracts.testing import (
    make_model_binding,
    make_tenant_id,
    make_tenant_principal,
    make_user_id,
    make_user_principal,
)
from factotum.model_gateway.bindings import InMemoryBindingRepository
from factotum.model_gateway.core import (
    DEFAULT_BASE_URLS,
    ModelGateway,
    default_adapters,
    guarded_clients,
)
from factotum.model_gateway.providers.base import EmbeddingBatch, ResolvedBinding
from factotum.model_gateway.retry import RetryPolicy
from factotum.model_gateway.secrets import EnvSecretResolver
from factotum.model_gateway.testing import (
    FakeServer,
    ScriptedAdapter,
    Sleeps,
    TrackedClients,
    ask,
    events,
    json_response,
    resolved,
)

TENANT = make_tenant_id()
ALICE = make_user_id()
SECRETS = EnvSecretResolver({"OPENAI_API_KEY": "sk-live-0123456789abcdefghijklmnop"})
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"city": {"type": "string"}},
    "required": ["city"],
}

TENANT_PRINCIPAL = make_tenant_principal(tenant_id=TENANT)
ALICE_PRINCIPAL = make_user_principal(tenant_id=TENANT, user_id=ALICE)


def local_binding(
    role: ModelRole = ModelRole.CHAT_MAIN,
    *,
    user: bool = False,
    model: str = "llama3.1:8b",
    options: BindingOptions | None = None,
) -> ModelBinding:
    return make_model_binding(
        tenant_id=TENANT,
        role=role,
        model=model,
        provider=ProviderKind.OLLAMA,
        base_url="http://localhost:11434",
        owner=Owner.of_subject(Subject.user(ALICE)) if user else None,
        options=options,
    )


def openai_binding(
    role: ModelRole = ModelRole.CHAT_MAIN,
    *,
    base_url: str | None = None,
    options: BindingOptions | None = None,
) -> ModelBinding:
    return make_model_binding(
        tenant_id=TENANT,
        role=role,
        model="gpt-4o-mini",
        provider=ProviderKind.OPENAI,
        base_url=base_url,
        secret_ref="env:OPENAI_API_KEY",
        options=options,
    )


class Rig:
    """A gateway wired to a scripted adapter, with what is needed to look at what it did."""

    def __init__(self, *bindings: ModelBinding, adapter: ScriptedAdapter | None = None) -> None:
        self.adapter = adapter or ScriptedAdapter()
        self.clients = TrackedClients()
        self.sleeps = Sleeps()
        adapters = dict.fromkeys(ProviderKind, self.adapter)
        self.gateway = ModelGateway(
            InMemoryBindingRepository(bindings),
            SECRETS,
            adapters=adapters,
            client_factory=self.clients,
            retry=RetryPolicy(attempts=3, base_delay_s=1.0, max_delay_s=8.0),
            sleep=self.sleeps,
            jitter=lambda: 1.0,
        )

    @property
    def last_resolved(self) -> ResolvedBinding:
        return self.adapter.chats[-1][0]

    def all_clients_closed(self) -> bool:
        return bool(self.clients.created) and all(c.is_closed for c in self.clients.created)


def reply(text: str = "Hello!", usage: Usage | None = None) -> ChatResult:
    return ChatResult(text=text, finish_reason=FinishReason.STOP, usage=usage)


class TestChat:
    async def test_the_answer_of_the_adapter_is_returned(self) -> None:
        rig = Rig(local_binding())
        rig.adapter.will_chat(reply("Hi!", Usage(input_tokens=3, output_tokens=2)))

        result = await rig.gateway.chat(TENANT_PRINCIPAL, ask())

        assert result == reply("Hi!", Usage(input_tokens=3, output_tokens=2))

    async def test_the_adapter_gets_the_request_as_it_was(self) -> None:
        rig = Rig(local_binding())
        rig.adapter.will_chat(reply())
        request = ask("What?", max_output_tokens=50)

        await rig.gateway.chat(TENANT_PRINCIPAL, request)

        assert rig.adapter.chats[0][1] == request

    async def test_the_binding_is_resolved_with_its_address(self) -> None:
        rig = Rig(local_binding())
        rig.adapter.will_chat(reply())

        await rig.gateway.chat(TENANT_PRINCIPAL, ask())

        assert rig.last_resolved.base_url == "http://localhost:11434"
        assert rig.last_resolved.model == "llama3.1:8b"
        assert rig.last_resolved.api_key is None

    async def test_the_key_is_read_only_now_and_handed_to_the_adapter(self) -> None:
        rig = Rig(openai_binding())
        rig.adapter.will_chat(reply())

        await rig.gateway.chat(TENANT_PRINCIPAL, ask())

        key = rig.last_resolved.api_key
        assert key is not None
        assert key.get_secret_value() == "sk-live-0123456789abcdefghijklmnop"

    @pytest.mark.parametrize(
        ("provider", "address"),
        [
            (ProviderKind.OPENAI, "https://api.openai.com/v1"),
            (ProviderKind.ANTHROPIC, "https://api.anthropic.com"),
            (ProviderKind.GEMINI, "https://generativelanguage.googleapis.com"),
        ],
    )
    async def test_hosted_providers_have_a_well_known_address(
        self, provider: ProviderKind, address: str
    ) -> None:
        binding = make_model_binding(
            tenant_id=TENANT,
            provider=provider,
            model="m",
            base_url=None,
            secret_ref="env:OPENAI_API_KEY",
        )
        rig = Rig(binding)
        rig.adapter.will_chat(reply())

        await rig.gateway.chat(TENANT_PRINCIPAL, ask())

        assert rig.last_resolved.base_url == address
        assert DEFAULT_BASE_URLS[provider] == address

    async def test_the_address_the_user_gave_replaces_the_default(self) -> None:
        rig = Rig(openai_binding(base_url="https://proxy.example.org/v1"))
        rig.adapter.will_chat(reply())

        await rig.gateway.chat(TENANT_PRINCIPAL, ask())

        assert rig.last_resolved.base_url == "https://proxy.example.org/v1"

    async def test_the_user_is_served_by_their_own_binding_and_the_tenant_device_by_the_tenants(
        self,
    ) -> None:
        rig = Rig(local_binding(model="tenant-model"), local_binding(user=True, model="own-model"))
        rig.adapter.will_chat(reply(), reply())

        await rig.gateway.chat(ALICE_PRINCIPAL, ask())
        assert rig.last_resolved.model == "own-model"

        await rig.gateway.chat(TENANT_PRINCIPAL, ask())
        assert rig.last_resolved.model == "tenant-model"

    async def test_a_fast_request_is_served_by_the_main_model_when_there_is_no_fast_one(
        self,
    ) -> None:
        rig = Rig(local_binding(ModelRole.CHAT_MAIN, model="main"))
        rig.adapter.will_chat(reply())

        await rig.gateway.chat(TENANT_PRINCIPAL, ask(role=ModelRole.CHAT_FAST))

        assert rig.last_resolved.model == "main"

    async def test_no_binding_is_a_typed_error_before_any_client_exists(self) -> None:
        rig = Rig()

        with pytest.raises(BindingNotFoundError):
            await rig.gateway.chat(TENANT_PRINCIPAL, ask())

        assert rig.clients.created == []

    async def test_a_missing_secret_is_a_typed_error_before_any_client_exists(self) -> None:
        binding = make_model_binding(
            tenant_id=TENANT,
            provider=ProviderKind.OPENAI,
            base_url=None,
            secret_ref="env:NOT_SET_ANYWHERE",
        )
        rig = Rig(binding)

        with pytest.raises(SecretNotFoundError):
            await rig.gateway.chat(TENANT_PRINCIPAL, ask())

        assert rig.clients.created == []
        assert rig.adapter.chats == []

    async def test_the_client_is_closed_after_the_call(self) -> None:
        rig = Rig(local_binding())
        rig.adapter.will_chat(reply())

        await rig.gateway.chat(TENANT_PRINCIPAL, ask())

        assert rig.all_clients_closed()

    async def test_the_client_is_closed_after_a_failure_too(self) -> None:
        rig = Rig(local_binding())
        rig.adapter.will_chat(ProviderAuthError("no"))

        with pytest.raises(ProviderAuthError):
            await rig.gateway.chat(TENANT_PRINCIPAL, ask())

        assert rig.all_clients_closed()


class TestTheGatewayDoesNotJudgeWhereAModelRuns:
    """Where the data goes is decided by the deployment and the providers the user bound
    (ADR 0010). The gateway serves the request with the model it was told to use."""

    async def test_a_hosted_model_serves_a_request_like_any_other(self) -> None:
        rig = Rig(openai_binding())
        rig.adapter.will_chat(reply())

        result = await rig.gateway.chat(TENANT_PRINCIPAL, ask())

        assert result.text == "Hello!"

    async def test_a_hosted_provider_behind_a_proxy_on_the_lan_is_served_too(self) -> None:
        rig = Rig(openai_binding(base_url="http://192.168.1.10:4000/v1"))
        rig.adapter.will_chat(reply())

        await rig.gateway.chat(TENANT_PRINCIPAL, ask())

        assert rig.last_resolved.base_url == "http://192.168.1.10:4000/v1"

    async def test_a_user_may_use_a_hosted_chat_model_and_a_local_embedding_model(self) -> None:
        rig = Rig(openai_binding(), local_binding(ModelRole.EMBEDDING))
        rig.adapter.will_chat(reply())
        rig.adapter.will_embed(EmbeddingBatch(vectors=((0.1, 0.2),)))

        await rig.gateway.chat(TENANT_PRINCIPAL, ask())
        await rig.gateway.embed(TENANT_PRINCIPAL, EmbeddingRequest(inputs=("a",)))

        assert len(rig.adapter.chats) == len(rig.adapter.embeds) == 1


class TestRetries:
    async def test_a_transient_failure_is_retried_after_a_pause(self) -> None:
        rig = Rig(local_binding())
        rig.adapter.will_chat(ProviderUnavailableError("busy"), reply("Second time lucky"))

        result = await rig.gateway.chat(TENANT_PRINCIPAL, ask())

        assert result.text == "Second time lucky"
        assert len(rig.adapter.chats) == 2
        assert rig.sleeps.delays == [1.0]

    async def test_the_pause_grows_with_every_failure(self) -> None:
        rig = Rig(local_binding())
        rig.adapter.will_chat(ProviderUnavailableError("a"), ProviderUnavailableError("b"), reply())

        await rig.gateway.chat(TENANT_PRINCIPAL, ask())

        assert rig.sleeps.delays == [1.0, 2.0]

    async def test_it_gives_up_after_the_attempts_and_raises_the_last_failure(self) -> None:
        rig = Rig(local_binding())
        rig.adapter.will_chat(*[ProviderUnavailableError(f"try {i}") for i in range(1, 4)])

        with pytest.raises(ProviderUnavailableError, match="try 3"):
            await rig.gateway.chat(TENANT_PRINCIPAL, ask())

        assert len(rig.adapter.chats) == 3

    async def test_a_rate_limit_waits_as_long_as_the_provider_asked(self) -> None:
        rig = Rig(local_binding())
        rig.adapter.will_chat(RateLimitedError("slow down", retry_after_s=3.0), reply())

        await rig.gateway.chat(TENANT_PRINCIPAL, ask())

        assert rig.sleeps.delays == [3.0]

    async def test_a_rate_limit_that_asks_for_a_long_wait_is_raised_at_once(self) -> None:
        rig = Rig(local_binding())
        rig.adapter.will_chat(RateLimitedError("slow down", retry_after_s=60.0))

        with pytest.raises(RateLimitedError) as caught:
            await rig.gateway.chat(TENANT_PRINCIPAL, ask())

        assert caught.value.retry_after_s == 60.0
        assert rig.sleeps.delays == []

    async def test_a_failure_that_waiting_will_not_fix_is_raised_at_once(self) -> None:
        rig = Rig(local_binding())
        rig.adapter.will_chat(ProviderAuthError("bad key"), reply())

        with pytest.raises(ProviderAuthError):
            await rig.gateway.chat(TENANT_PRINCIPAL, ask())

        assert len(rig.adapter.chats) == 1
        assert rig.sleeps.delays == []

    async def test_embeddings_are_retried_too(self) -> None:
        rig = Rig(local_binding(ModelRole.EMBEDDING))
        rig.adapter.will_embed(
            ProviderUnavailableError("busy"), EmbeddingBatch(vectors=((0.1, 0.2),))
        )

        result = await rig.gateway.embed(TENANT_PRINCIPAL, EmbeddingRequest(inputs=("a",)))

        assert result.vectors == ((0.1, 0.2),)
        assert len(rig.adapter.embeds) == 2

    async def test_a_new_client_is_not_needed_for_a_retry_and_it_is_closed_once(self) -> None:
        rig = Rig(local_binding())
        rig.adapter.will_chat(ProviderUnavailableError("busy"), reply())

        await rig.gateway.chat(TENANT_PRINCIPAL, ask())

        assert len(rig.clients.created) == 1
        assert rig.all_clients_closed()


class TestStructuredOutput:
    async def test_a_valid_answer_comes_back_parsed(self) -> None:
        rig = Rig(local_binding())
        rig.adapter.will_chat(reply('{"city": "Rome"}'))

        result = await rig.gateway.chat(TENANT_PRINCIPAL, ask(response_schema=SCHEMA))

        assert result.parsed == {"city": "Rome"}

    async def test_a_provider_with_native_support_is_handed_the_schema(self) -> None:
        rig = Rig(local_binding(), adapter=ScriptedAdapter(native=True))
        rig.adapter.will_chat(reply('{"city": "Rome"}'))

        await rig.gateway.chat(TENANT_PRINCIPAL, ask(response_schema=SCHEMA))

        assert rig.adapter.chats[0][1].response_schema == SCHEMA

    async def test_a_provider_without_it_is_asked_for_json_in_the_prompt_instead(self) -> None:
        rig = Rig(local_binding(), adapter=ScriptedAdapter(native=False))
        rig.adapter.will_chat(reply('{"city": "Rome"}'))

        await rig.gateway.chat(TENANT_PRINCIPAL, ask(response_schema=SCHEMA))

        sent = rig.adapter.chats[0][1]
        assert sent.response_schema is None
        assert "JSON Schema" in sent.messages[0].content

    async def test_an_invalid_answer_is_repaired_by_asking_again(self) -> None:
        rig = Rig(local_binding())
        rig.adapter.will_chat(reply("{}"), reply('{"city": "Rome"}'))

        result = await rig.gateway.chat(TENANT_PRINCIPAL, ask(response_schema=SCHEMA))

        assert result.parsed == {"city": "Rome"}
        assert len(rig.adapter.chats) == 2

    async def test_a_model_that_never_gets_it_right_ends_in_a_typed_error(self) -> None:
        rig = Rig(local_binding())
        rig.adapter.will_chat(reply("{}"), reply("{}"), reply("{}"))

        with pytest.raises(InvalidStructuredOutputError):
            await rig.gateway.chat(TENANT_PRINCIPAL, ask(response_schema=SCHEMA))

    async def test_retries_and_repairs_are_independent(self) -> None:
        rig = Rig(local_binding())
        rig.adapter.will_chat(
            ProviderUnavailableError("busy"), reply("{}"), reply('{"city": "Rome"}')
        )

        result = await rig.gateway.chat(TENANT_PRINCIPAL, ask(response_schema=SCHEMA))

        assert result.parsed == {"city": "Rome"}
        assert len(rig.adapter.chats) == 3

    async def test_a_refusal_is_a_typed_error(self) -> None:
        rig = Rig(local_binding())
        rig.adapter.will_chat(ChatResult(text="No.", finish_reason=FinishReason.CONTENT_FILTER))

        with pytest.raises(ContentFilteredError):
            await rig.gateway.chat(TENANT_PRINCIPAL, ask(response_schema=SCHEMA))


HELLO: list[EventUnion] = [
    TextDelta(text="Hel"),
    TextDelta(text="lo"),
    UsageEvent(usage=Usage(input_tokens=3, output_tokens=2)),
    FinishEvent(reason=FinishReason.STOP),
]


class TestStreaming:
    async def test_the_events_of_the_adapter_are_passed_on_in_order(self) -> None:
        rig = Rig(local_binding())
        rig.adapter.will_stream(*HELLO)

        got = await events(rig.gateway.chat_stream(TENANT_PRINCIPAL, ask(stream=True)))

        assert got == HELLO

    async def test_the_client_is_closed_when_the_stream_ends(self) -> None:
        rig = Rig(local_binding())
        rig.adapter.will_stream(*HELLO)

        await events(rig.gateway.chat_stream(TENANT_PRINCIPAL, ask(stream=True)))

        assert rig.all_clients_closed()
        assert rig.adapter.stream_endings == ["completed"]

    async def test_a_failure_before_the_first_event_is_raised_typed(self) -> None:
        rig = Rig(local_binding())
        rig.adapter.will_stream(ProviderAuthError("bad key"))

        with pytest.raises(ProviderAuthError):
            await events(rig.gateway.chat_stream(TENANT_PRINCIPAL, ask(stream=True)))

        assert rig.all_clients_closed()

    async def test_a_failure_to_start_is_retried_and_the_caller_never_notices(self) -> None:
        rig = Rig(local_binding())
        rig.adapter.will_stream(ProviderUnavailableError("busy"))
        rig.adapter.will_stream(*HELLO)

        got = await events(rig.gateway.chat_stream(TENANT_PRINCIPAL, ask(stream=True)))

        assert got == HELLO
        assert len(rig.adapter.streams) == 2
        assert rig.sleeps.delays == [1.0]
        assert rig.adapter.stream_endings == ["failed", "completed"]

    async def test_a_rate_limit_at_the_start_is_raised_when_the_wait_is_too_long(self) -> None:
        rig = Rig(local_binding())
        rig.adapter.will_stream(RateLimitedError("slow", retry_after_s=60.0))

        with pytest.raises(RateLimitedError):
            await events(rig.gateway.chat_stream(TENANT_PRINCIPAL, ask(stream=True)))

    async def test_a_failure_after_the_first_event_ends_the_stream_with_an_error_event(
        self,
    ) -> None:
        rig = Rig(local_binding())
        rig.adapter.will_stream(
            TextDelta(text="Hel"), ProviderUnavailableError("the model crashed")
        )

        got = await events(rig.gateway.chat_stream(TENANT_PRINCIPAL, ask(stream=True)))

        assert got == [
            TextDelta(text="Hel"),
            ErrorEvent(code="provider_unavailable", message="the model crashed"),
        ]

    async def test_the_code_of_the_error_event_is_the_code_of_the_error(self) -> None:
        rig = Rig(local_binding())
        rig.adapter.will_stream(TextDelta(text="Hel"), RateLimitedError("slow down"))

        got = await events(rig.gateway.chat_stream(TENANT_PRINCIPAL, ask(stream=True)))

        assert got[-1] == ErrorEvent(code="rate_limited", message="slow down")

    async def test_a_stream_that_has_started_is_never_retried(self) -> None:
        rig = Rig(local_binding())
        rig.adapter.will_stream(TextDelta(text="Hel"), ProviderUnavailableError("crash"))
        rig.adapter.will_stream(*HELLO)

        await events(rig.gateway.chat_stream(TENANT_PRINCIPAL, ask(stream=True)))

        assert len(rig.adapter.streams) == 1
        assert rig.sleeps.delays == []

    async def test_a_consumer_that_walks_away_closes_the_provider_stream(self) -> None:
        rig = Rig(local_binding())
        rig.adapter.will_stream(*HELLO)
        stream = rig.gateway.chat_stream(TENANT_PRINCIPAL, ask(stream=True))

        first = await anext(stream)
        await stream.aclose()

        assert first == TextDelta(text="Hel")
        assert rig.adapter.stream_endings == ["closed"]
        assert rig.all_clients_closed()

    async def test_a_stream_with_no_events_is_an_empty_stream_and_not_an_error(self) -> None:
        rig = Rig(local_binding())
        rig.adapter.will_stream()

        got = await events(rig.gateway.chat_stream(TENANT_PRINCIPAL, ask(stream=True)))

        assert got == []
        assert rig.all_clients_closed()

    async def test_no_binding_is_raised_when_the_stream_is_first_asked_for_an_event(self) -> None:
        rig = Rig()

        with pytest.raises(BindingNotFoundError):
            await events(rig.gateway.chat_stream(TENANT_PRINCIPAL, ask(stream=True)))


class TestEmbeddings:
    async def test_vectors_come_back_with_their_size_and_the_space_they_live_in(self) -> None:
        rig = Rig(local_binding(ModelRole.EMBEDDING, model="nomic-embed-text"))
        rig.adapter.will_embed(
            EmbeddingBatch(vectors=((0.1, 0.2, 0.3), (0.4, 0.5, 0.6)), usage=Usage(input_tokens=7))
        )

        result = await rig.gateway.embed(TENANT_PRINCIPAL, EmbeddingRequest(inputs=("a", "b")))

        assert result.vectors == ((0.1, 0.2, 0.3), (0.4, 0.5, 0.6))
        assert result.dimensions == 3
        assert result.usage == Usage(input_tokens=7)
        expected = EmbeddingSpace(
            provider=ProviderKind.OLLAMA, model="nomic-embed-text", dimensions=3
        )
        assert result.space_id == expected.id

    async def test_the_adapter_gets_the_inputs_and_the_client_is_closed(self) -> None:
        rig = Rig(local_binding(ModelRole.EMBEDDING))
        rig.adapter.will_embed(EmbeddingBatch(vectors=((0.1,), (0.2,))))

        await rig.gateway.embed(TENANT_PRINCIPAL, EmbeddingRequest(inputs=("first", "second")))

        assert rig.adapter.embeds[0][1] == ("first", "second")
        assert rig.all_clients_closed()

    async def test_a_different_model_lives_in_a_different_space(self) -> None:
        first = Rig(local_binding(ModelRole.EMBEDDING, model="model-a"))
        second = Rig(local_binding(ModelRole.EMBEDDING, model="model-b"))
        for rig in (first, second):
            rig.adapter.will_embed(EmbeddingBatch(vectors=((0.1, 0.2),)))

        one = await first.gateway.embed(TENANT_PRINCIPAL, EmbeddingRequest(inputs=("a",)))
        other = await second.gateway.embed(TENANT_PRINCIPAL, EmbeddingRequest(inputs=("a",)))

        assert one.space_id != other.space_id

    async def test_there_is_no_fallback_from_a_chat_model(self) -> None:
        rig = Rig(local_binding(ModelRole.CHAT_MAIN))

        with pytest.raises(BindingNotFoundError, match="embedding"):
            await rig.gateway.embed(TENANT_PRINCIPAL, EmbeddingRequest(inputs=("a",)))

    async def test_fewer_vectors_than_inputs_is_a_provider_failure(self) -> None:
        rig = Rig(local_binding(ModelRole.EMBEDDING))
        rig.adapter.will_embed(EmbeddingBatch(vectors=((0.1,),)))

        with pytest.raises(ProviderUnavailableError, match="1 vectors for 2 inputs"):
            await rig.gateway.embed(TENANT_PRINCIPAL, EmbeddingRequest(inputs=("a", "b")))

    @pytest.mark.parametrize(
        "vectors", [((0.1, 0.2), (0.3,)), ((), ())], ids=["different-sizes", "empty"]
    )
    async def test_vectors_that_do_not_agree_on_a_size_are_a_provider_failure(
        self, vectors: tuple[tuple[float, ...], ...]
    ) -> None:
        rig = Rig(local_binding(ModelRole.EMBEDDING))
        rig.adapter.will_embed(EmbeddingBatch(vectors=vectors))

        with pytest.raises(ProviderUnavailableError, match="different sizes, or empty"):
            await rig.gateway.embed(TENANT_PRINCIPAL, EmbeddingRequest(inputs=("a", "b")))

    async def test_a_size_that_is_not_the_one_the_binding_declares_is_reported(self) -> None:
        declared = local_binding(ModelRole.EMBEDDING, options=BindingOptions(dimensions=512))
        rig = Rig(declared)
        rig.adapter.will_embed(EmbeddingBatch(vectors=((0.1, 0.2),)))

        with pytest.raises(InvalidRequestError, match="declares 512 dimensions"):
            await rig.gateway.embed(TENANT_PRINCIPAL, EmbeddingRequest(inputs=("a",)))

    async def test_the_declared_size_is_accepted_when_the_model_agrees(self) -> None:
        declared = local_binding(ModelRole.EMBEDDING, options=BindingOptions(dimensions=2))
        rig = Rig(declared)
        rig.adapter.will_embed(EmbeddingBatch(vectors=((0.1, 0.2),)))

        result = await rig.gateway.embed(TENANT_PRINCIPAL, EmbeddingRequest(inputs=("a",)))

        assert result.dimensions == 2

    async def test_a_provider_without_embeddings_is_refused_before_any_request(self) -> None:
        anthropic = make_model_binding(
            tenant_id=TENANT,
            role=ModelRole.EMBEDDING,
            provider=ProviderKind.ANTHROPIC,
            base_url=None,
            secret_ref="env:OPENAI_API_KEY",
        )
        gateway = ModelGateway(
            InMemoryBindingRepository([anthropic]), SECRETS, client_factory=TrackedClients()
        )

        with pytest.raises(RoleNotSupportedError):
            await gateway.embed(TENANT_PRINCIPAL, EmbeddingRequest(inputs=("a",)))


class TestProfile:
    async def test_a_profile_describes_the_role_without_naming_the_model(self) -> None:
        options = BindingOptions(max_input_tokens=32_000, max_output_tokens=2048)
        rig = Rig(local_binding(model="secret-model-name", options=options))

        profile = await rig.gateway.profile(TENANT_PRINCIPAL, ModelRole.CHAT_MAIN)

        assert profile.role is ModelRole.CHAT_MAIN
        assert profile.max_input_tokens == 32_000
        assert profile.max_output_tokens == 2048
        assert profile.streaming is True
        assert "secret-model-name" not in profile.model_dump_json()

    @pytest.mark.parametrize(
        ("native", "expected"),
        [(True, StructuredOutputSupport.NATIVE), (False, StructuredOutputSupport.EMULATED)],
    )
    async def test_it_says_how_structured_output_is_supported(
        self, native: bool, expected: StructuredOutputSupport
    ) -> None:
        rig = Rig(local_binding(), adapter=ScriptedAdapter(native=native))

        profile = await rig.gateway.profile(TENANT_PRINCIPAL, ModelRole.CHAT_MAIN)

        assert profile.structured_output is expected

    async def test_an_embedding_role_does_not_stream_and_has_no_structured_output(self) -> None:
        rig = Rig(local_binding(ModelRole.EMBEDDING))

        profile = await rig.gateway.profile(TENANT_PRINCIPAL, ModelRole.EMBEDDING)

        assert profile.streaming is False
        assert profile.structured_output is None

    async def test_the_role_asked_for_is_the_role_reported_even_when_a_fallback_serves_it(
        self,
    ) -> None:
        rig = Rig(local_binding(ModelRole.CHAT_MAIN, options=BindingOptions(max_input_tokens=4096)))

        profile = await rig.gateway.profile(TENANT_PRINCIPAL, ModelRole.CHAT_FAST)

        assert profile.role is ModelRole.CHAT_FAST
        assert profile.max_input_tokens == 4096

    async def test_the_user_sees_the_profile_of_their_own_binding(self) -> None:
        small = BindingOptions(max_input_tokens=2048)
        rig = Rig(local_binding(), local_binding(user=True, options=small))

        mine = await rig.gateway.profile(ALICE_PRINCIPAL, ModelRole.CHAT_MAIN)
        shared = await rig.gateway.profile(TENANT_PRINCIPAL, ModelRole.CHAT_MAIN)

        assert (mine.max_input_tokens, shared.max_input_tokens) == (2048, 8192)

    async def test_a_profile_does_not_need_the_key_to_be_available(self) -> None:
        binding = make_model_binding(
            tenant_id=TENANT, provider=ProviderKind.OPENAI, base_url=None, secret_ref="env:NOPE"
        )
        rig = Rig(binding)

        profile = await rig.gateway.profile(TENANT_PRINCIPAL, ModelRole.CHAT_MAIN)

        assert profile.role is ModelRole.CHAT_MAIN

    async def test_no_binding_is_a_typed_error(self) -> None:
        with pytest.raises(BindingNotFoundError):
            await Rig().gateway.profile(TENANT_PRINCIPAL, ModelRole.CHAT_MAIN)


class TestLogging:
    async def test_a_call_leaves_one_line_about_who_asked_what_of_which_model(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        rig = Rig(local_binding())
        rig.adapter.will_chat(reply("Hi!", Usage(input_tokens=9, output_tokens=4)))

        with caplog.at_level(logging.INFO, logger="factotum.model_gateway"):
            await rig.gateway.chat(TENANT_PRINCIPAL, ask())

        [record] = caplog.records
        assert record.levelno == logging.INFO
        assert record.__dict__["role"] == "chat.main"
        assert record.__dict__["provider"] == "ollama"
        assert record.__dict__["model"] == "llama3.1:8b"
        assert record.__dict__["outcome"] == "ok"
        assert record.__dict__["tenant_id"] == str(TENANT)
        assert record.__dict__["input_tokens"] == 9
        assert record.__dict__["output_tokens"] == 4
        assert record.__dict__["duration_ms"] >= 0

    async def test_the_conversation_is_never_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        rig = Rig(local_binding())
        rig.adapter.will_chat(reply("The launch code is 0000."))

        with caplog.at_level(logging.DEBUG):
            await rig.gateway.chat(TENANT_PRINCIPAL, ask("What is the launch code?"))

        text = " ".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
        assert "launch code" not in text

    async def test_the_key_is_never_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        rig = Rig(openai_binding())
        rig.adapter.will_chat(reply())

        with caplog.at_level(logging.DEBUG):
            await rig.gateway.chat(TENANT_PRINCIPAL, ask())

        text = " ".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
        assert "sk-live" not in text

    async def test_a_failure_is_logged_with_its_code_as_a_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        rig = Rig(local_binding())
        rig.adapter.will_chat(ProviderAuthError("bad key"))

        with (
            caplog.at_level(logging.INFO, logger="factotum.model_gateway"),
            pytest.raises(ProviderAuthError),
        ):
            await rig.gateway.chat(TENANT_PRINCIPAL, ask())

        [record] = caplog.records
        assert record.levelno == logging.WARNING
        assert record.__dict__["outcome"] == "provider_auth_error"

    async def test_a_call_that_never_got_a_binding_is_logged_without_a_model(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            caplog.at_level(logging.INFO, logger="factotum.model_gateway"),
            pytest.raises(BindingNotFoundError),
        ):
            await Rig().gateway.chat(TENANT_PRINCIPAL, ask())

        [record] = caplog.records
        assert record.__dict__["outcome"] == "binding_not_found"
        assert "model" not in record.__dict__
        assert "provider" not in record.__dict__

    async def test_a_stream_is_logged_when_it_ends_with_its_usage(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        rig = Rig(local_binding())
        rig.adapter.will_stream(*HELLO)

        with caplog.at_level(logging.INFO, logger="factotum.model_gateway"):
            await events(rig.gateway.chat_stream(TENANT_PRINCIPAL, ask(stream=True)))

        [record] = caplog.records
        assert record.__dict__["outcome"] == "ok"
        assert record.__dict__["input_tokens"] == 3

    async def test_a_stream_that_the_consumer_abandons_is_logged_as_cancelled(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        rig = Rig(local_binding())
        rig.adapter.will_stream(*HELLO)

        with caplog.at_level(logging.INFO, logger="factotum.model_gateway"):
            stream = rig.gateway.chat_stream(TENANT_PRINCIPAL, ask(stream=True))
            await anext(stream)
            await stream.aclose()

        [record] = caplog.records
        assert record.levelno == logging.INFO
        assert record.__dict__["outcome"] == "cancelled"

    async def test_a_stream_that_fails_midway_is_logged_with_the_error_code(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        rig = Rig(local_binding())
        rig.adapter.will_stream(TextDelta(text="Hel"), ProviderUnavailableError("crash"))

        with caplog.at_level(logging.INFO, logger="factotum.model_gateway"):
            await events(rig.gateway.chat_stream(TENANT_PRINCIPAL, ask(stream=True)))

        [record] = caplog.records
        assert record.levelno == logging.WARNING
        assert record.__dict__["outcome"] == "provider_unavailable"

    async def test_an_embedding_call_is_logged_too(self, caplog: pytest.LogCaptureFixture) -> None:
        rig = Rig(local_binding(ModelRole.EMBEDDING))
        rig.adapter.will_embed(EmbeddingBatch(vectors=((0.1,),), usage=Usage(input_tokens=4)))

        with caplog.at_level(logging.INFO, logger="factotum.model_gateway"):
            await rig.gateway.embed(TENANT_PRINCIPAL, EmbeddingRequest(inputs=("a",)))

        [record] = caplog.records
        assert record.__dict__["role"] == "embedding"
        assert record.__dict__["input_tokens"] == 4


class TestAgainstARealAdapter:
    """The gateway with a real adapter and a fake server: the whole path but the network."""

    @staticmethod
    def gateway(server: FakeServer, *bindings: ModelBinding) -> ModelGateway:
        return ModelGateway(
            InMemoryBindingRepository(bindings),
            SECRETS,
            client_factory=lambda resolved: server.client,
        )

    async def test_a_local_ollama_answers(self) -> None:
        server = FakeServer(
            json_response(
                {
                    "message": {"role": "assistant", "content": "Hi!"},
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 5,
                    "eval_count": 2,
                }
            )
        )
        gateway = self.gateway(server, local_binding())

        result = await gateway.chat(TENANT_PRINCIPAL, ask("Hello"))

        assert result.text == "Hi!"
        assert result.usage == Usage(input_tokens=5, output_tokens=2)
        assert str(server.request.url) == "http://localhost:11434/api/chat"

    async def test_a_hosted_provider_is_called_with_its_key_in_the_header(self) -> None:
        server = FakeServer(
            json_response({"choices": [{"message": {"content": "Hi!"}, "finish_reason": "stop"}]})
        )
        gateway = self.gateway(server, openai_binding())

        await gateway.chat(TENANT_PRINCIPAL, ask("Hello"))

        assert str(server.request.url) == "https://api.openai.com/v1/chat/completions"
        assert (
            server.request.headers["authorization"] == "Bearer sk-live-0123456789abcdefghijklmnop"
        )

    async def test_a_provider_error_reaches_the_caller_typed_and_without_the_key(self) -> None:
        server = FakeServer(
            json_response(
                {"error": {"message": "Incorrect API key: sk-live-0123456789abcdefghijklmnop"}},
                status=401,
            )
        )
        gateway = self.gateway(server, openai_binding())

        with pytest.raises(ProviderAuthError) as caught:
            await gateway.chat(TENANT_PRINCIPAL, ask("Hello"))

        assert "0123456789abcdef" not in str(caught.value)


class TestDefaults:
    def test_every_provider_has_an_adapter(self) -> None:
        adapters = default_adapters()

        assert set(adapters) == set(ProviderKind)
        assert all(adapter.kind is kind for kind, adapter in adapters.items())

    def test_the_adapters_are_not_shared_between_gateways(self) -> None:
        assert (
            default_adapters()[ProviderKind.OLLAMA] is not default_adapters()[ProviderKind.OLLAMA]
        )


def resolved_with(timeout_s: float = 60.0) -> ResolvedBinding:
    binding = make_model_binding(tenant_id=TENANT, options=BindingOptions(timeout_s=timeout_s))
    return resolved(binding, base_url="http://localhost:11434")


class TestTheClientsOfADeployment:
    """The last wall: which networks a binding may be pointed at is decided by the deployment."""

    async def test_a_deployment_that_shares_its_network_reaches_the_public_internet_only(
        self,
    ) -> None:
        client = guarded_clients(UrlPolicy.public_only())(resolved_with())

        async with client:
            with pytest.raises(UrlNotAllowedError):
                await client.get("http://127.0.0.1:9/")

    async def test_an_on_premise_deployment_may_reach_its_own_network(self) -> None:
        client = guarded_clients(UrlPolicy.private_networks_allowed())(resolved_with())

        async with client:
            with pytest.raises(httpx.ConnectError):  # allowed, and nothing is listening
                await client.get("http://127.0.0.1:9/")

    async def test_a_gateway_that_was_not_told_otherwise_reaches_the_public_internet_only(
        self,
    ) -> None:
        # The default is the safe one: an on-premise deployment has to ask for the private network.
        binding = local_binding()
        gateway = ModelGateway(InMemoryBindingRepository([binding]), SECRETS)

        with pytest.raises(UrlNotAllowedError):
            await gateway.chat(TENANT_PRINCIPAL, ask())

    @pytest.mark.parametrize(
        "policy", [UrlPolicy.public_only(), UrlPolicy.private_networks_allowed()]
    )
    async def test_nobody_may_reach_the_cloud_metadata_service(self, policy: UrlPolicy) -> None:
        client = guarded_clients(policy)(resolved_with())

        async with client:
            with pytest.raises(UrlNotAllowedError):
                await client.get("http://169.254.169.254/latest/meta-data/")

    async def test_redirects_are_not_followed(self) -> None:
        client = guarded_clients(UrlPolicy.private_networks_allowed())(resolved_with())

        async with client:
            assert client.follow_redirects is False

    async def test_the_timeout_of_the_binding_applies_and_connecting_gets_less_of_it(self) -> None:
        client = guarded_clients(UrlPolicy.private_networks_allowed())(resolved_with(120.0))

        async with client:
            assert client.timeout.read == 120.0
            assert client.timeout.connect == 10.0

    async def test_a_short_timeout_shortens_the_connection_timeout_too(self) -> None:
        client = guarded_clients(UrlPolicy.private_networks_allowed())(resolved_with(3.0))

        async with client:
            assert client.timeout.connect == 3.0
