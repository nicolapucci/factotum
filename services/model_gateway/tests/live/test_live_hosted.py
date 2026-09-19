"""The hosted providers, with the real adapters and the real internet.

Opt-in. A provider is tested only if its key is in the environment, so nothing is spent and nothing
fails for a provider you do not use:

    export OPENAI_API_KEY=... ANTHROPIC_API_KEY=... GEMINI_API_KEY=...
    FACTOTUM_LIVE_TESTS=1 uv run pytest -m live services/model_gateway/tests/live -k hosted

The models can be changed with FACTOTUM_LIVE_OPENAI_MODEL, FACTOTUM_LIVE_ANTHROPIC_MODEL and
FACTOTUM_LIVE_GEMINI_MODEL; the embedding models with FACTOTUM_LIVE_OPENAI_EMBED_MODEL and
FACTOTUM_LIVE_GEMINI_EMBED_MODEL. Each test costs a few tokens.

One test needs no key: it sends a key that cannot be valid and checks that each provider's refusal
comes back as the same typed error.
"""

import os
from dataclasses import dataclass

import pytest
from _pytest.mark import ParameterSet

from factotum.contracts.chat import (
    ChatMessage,
    ChatRequest,
    FinishEvent,
    FinishReason,
    MessageRole,
    TextDelta,
)
from factotum.contracts.embeddings import EmbeddingRequest
from factotum.contracts.errors import ProviderAuthError
from factotum.contracts.models import BindingOptions, ModelRole, ProviderKind
from factotum.contracts.testing import make_model_binding, make_tenant_id, make_tenant_principal
from factotum.model_gateway.bindings import InMemoryBindingRepository
from factotum.model_gateway.core import ModelGateway
from factotum.model_gateway.secrets import EnvSecretResolver

pytestmark = [
    pytest.mark.live,
    # The unit tests may only reach the loopback interface; these need the internet.
    pytest.mark.enable_socket,
    pytest.mark.skipif(
        os.environ.get("FACTOTUM_LIVE_TESTS") != "1",
        reason="live tests are opt-in: set FACTOTUM_LIVE_TESTS=1",
    ),
]


@dataclass(frozen=True)
class Hosted:
    provider: ProviderKind
    key_variable: str
    model: str
    embedding_model: str | None


def hosted(provider: ProviderKind, key_variable: str, model: str, embedding: str | None) -> Hosted:
    prefix = f"FACTOTUM_LIVE_{provider.value.upper()}"
    return Hosted(
        provider,
        key_variable,
        os.environ.get(f"{prefix}_MODEL", model),
        os.environ.get(f"{prefix}_EMBED_MODEL", embedding) if embedding else None,
    )


PROVIDERS = [
    hosted(ProviderKind.OPENAI, "OPENAI_API_KEY", "gpt-4o-mini", "text-embedding-3-small"),
    hosted(ProviderKind.ANTHROPIC, "ANTHROPIC_API_KEY", "claude-haiku-4-5-20251001", None),
    hosted(ProviderKind.GEMINI, "GEMINI_API_KEY", "gemini-2.5-flash", "gemini-embedding-001"),
]


def with_a_key(providers: list[Hosted]) -> list[ParameterSet]:
    """The providers, each skipped when its key is not in the environment."""
    return [
        pytest.param(
            provider,
            id=provider.provider.value,
            marks=pytest.mark.skipif(
                not os.environ.get(provider.key_variable),
                reason=f"{provider.key_variable} is not set",
            ),
        )
        for provider in providers
    ]


EVERY_PROVIDER = [pytest.param(provider, id=provider.provider.value) for provider in PROVIDERS]
WITH_A_KEY = with_a_key(PROVIDERS)
WITH_A_KEY_AND_EMBEDDINGS = with_a_key([p for p in PROVIDERS if p.embedding_model])

TENANT = make_tenant_id()
PRINCIPAL = make_tenant_principal(tenant_id=TENANT)


def gateway(
    provider: Hosted, *, key_variable: str | None = None, embedding: bool = False
) -> ModelGateway:
    binding = make_model_binding(
        tenant_id=TENANT,
        role=ModelRole.EMBEDDING if embedding else ModelRole.CHAT_MAIN,
        provider=provider.provider,
        model=(provider.embedding_model if embedding else provider.model) or provider.model,
        base_url=None,
        secret_ref=f"env:{key_variable or provider.key_variable}",
        options=BindingOptions(timeout_s=60),
    )
    return ModelGateway(InMemoryBindingRepository([binding]), EnvSecretResolver())


def ask(text: str, **fields: object) -> ChatRequest:
    return ChatRequest(
        messages=(ChatMessage(role=MessageRole.USER, content=text),),
        **fields,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("provider", EVERY_PROVIDER)
async def test_a_key_that_cannot_be_valid_is_the_same_typed_error_everywhere(
    provider: Hosted, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FACTOTUM_LIVE_BOGUS_KEY", "sk-this-key-is-not-valid-0123456789abcdef")

    with pytest.raises(ProviderAuthError) as caught:
        await gateway(provider, key_variable="FACTOTUM_LIVE_BOGUS_KEY").chat(
            PRINCIPAL, ask("Reply with one word.", max_output_tokens=5)
        )

    assert not caught.value.retryable
    assert caught.value.details["provider"] == provider.provider.value
    assert "this-key-is-not-valid" not in str(caught.value)


@pytest.mark.parametrize("provider", WITH_A_KEY)
async def test_the_provider_answers(provider: Hosted) -> None:
    result = await gateway(provider).chat(
        PRINCIPAL, ask("Reply with exactly one word: pong", max_output_tokens=200, temperature=0)
    )

    assert "pong" in result.text.lower()
    assert result.finish_reason is FinishReason.STOP
    assert result.usage is not None
    assert result.usage.input_tokens
    assert result.usage.output_tokens


@pytest.mark.parametrize("provider", WITH_A_KEY)
async def test_the_provider_streams(provider: Hosted) -> None:
    request = ask("Count from one to five in words.", max_output_tokens=200, temperature=0)

    events = [event async for event in gateway(provider).chat_stream(PRINCIPAL, request)]

    text = "".join(event.text for event in events if isinstance(event, TextDelta))
    assert "three" in text.lower()
    assert isinstance(events[-1], FinishEvent)


@pytest.mark.parametrize("provider", WITH_A_KEY)
async def test_the_provider_gives_structured_output(provider: Hosted) -> None:
    schema = {
        "type": "object",
        "properties": {"city": {"type": "string"}, "country": {"type": "string"}},
        "required": ["city", "country"],
        "additionalProperties": False,
    }

    result = await gateway(provider).chat(
        PRINCIPAL,
        ask(
            "Where is the Colosseum?", response_schema=schema, max_output_tokens=300, temperature=0
        ),
    )

    assert isinstance(result.parsed, dict)
    assert "rom" in result.parsed["city"].lower()


@pytest.mark.parametrize("provider", WITH_A_KEY)
async def test_the_provider_gives_structured_output_for_a_schema_outside_the_native_subset(
    provider: Hosted,
) -> None:
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "integer", "minimum": 0}},
        "required": ["answer"],
    }

    result = await gateway(provider).chat(
        PRINCIPAL,
        ask("What is 6 times 7?", response_schema=schema, max_output_tokens=300, temperature=0),
    )

    assert result.parsed == {"answer": 42}


@pytest.mark.parametrize("provider", WITH_A_KEY_AND_EMBEDDINGS)
async def test_the_provider_embeds(provider: Hosted) -> None:
    inputs = ("The cat sits on the mat.", "A kitten is resting on a rug.", "The tax report is due.")

    result = await gateway(provider, embedding=True).embed(
        PRINCIPAL, EmbeddingRequest(inputs=inputs)
    )

    assert len(result.vectors) == 3
    assert result.dimensions > 100
    assert result.space_id.startswith("emb-")
