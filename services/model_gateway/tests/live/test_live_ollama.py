"""The gateway against a real Ollama, with the real adapter, the real client and no fakes.

Opt-in, because it needs a running Ollama with the models below pulled and takes a while:

    FACTOTUM_LIVE_TESTS=1 uv run pytest -m live services/model_gateway/tests/live -k ollama

Which server and models to use can be changed with FACTOTUM_LIVE_OLLAMA_URL,
FACTOTUM_LIVE_OLLAMA_CHAT_MODEL and FACTOTUM_LIVE_OLLAMA_EMBED_MODEL.
"""

import math
import os
from typing import Any

import httpx
import pytest

from factotum.common.config import Environment, ServiceSettings
from factotum.common.devauth import DEV_TENANT_ID, DEV_USER_ID, DevAuthenticator
from factotum.contracts.chat import (
    ChatMessage,
    ChatRequest,
    FinishEvent,
    FinishReason,
    MessageRole,
    TextDelta,
    UsageEvent,
)
from factotum.contracts.embeddings import EmbeddingRequest
from factotum.contracts.errors import InvalidRequestError
from factotum.contracts.models import BindingOptions, ModelRole, ProviderKind
from factotum.contracts.testing import make_model_binding, make_tenant_principal
from factotum.model_gateway.api import create_app
from factotum.model_gateway.bindings import InMemoryBindingRepository
from factotum.model_gateway.core import ModelGateway
from factotum.model_gateway.secrets import EnvSecretResolver

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("FACTOTUM_LIVE_TESTS") != "1",
        reason="live tests are opt-in: set FACTOTUM_LIVE_TESTS=1 (and run Ollama)",
    ),
]

URL = os.environ.get("FACTOTUM_LIVE_OLLAMA_URL", "http://127.0.0.1:11434")
CHAT_MODEL = os.environ.get("FACTOTUM_LIVE_OLLAMA_CHAT_MODEL", "llama3.2:3b-instruct-q8_0")
EMBED_MODEL = os.environ.get("FACTOTUM_LIVE_OLLAMA_EMBED_MODEL", "nomic-embed-text")

PRINCIPAL = make_tenant_principal(tenant_id=DEV_TENANT_ID)


def gateway(*, options: BindingOptions | None = None) -> ModelGateway:
    chat = make_model_binding(
        tenant_id=DEV_TENANT_ID,
        role=ModelRole.CHAT_MAIN,
        provider=ProviderKind.OLLAMA,
        model=CHAT_MODEL,
        base_url=URL,
        options=options or BindingOptions(timeout_s=300),
    )
    embedding = make_model_binding(
        tenant_id=DEV_TENANT_ID,
        role=ModelRole.EMBEDDING,
        provider=ProviderKind.OLLAMA,
        model=EMBED_MODEL,
        base_url=URL,
        options=BindingOptions(timeout_s=300),
    )
    return ModelGateway(InMemoryBindingRepository([chat, embedding]), EnvSecretResolver())


def ask(text: str, **fields: Any) -> ChatRequest:
    return ChatRequest(messages=(ChatMessage(role=MessageRole.USER, content=text),), **fields)


def cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


async def test_a_real_model_answers() -> None:
    request = ask("Reply with exactly one word: pong", max_output_tokens=20, temperature=0)

    result = await gateway().chat(PRINCIPAL, request)

    assert "pong" in result.text.lower()
    assert result.finish_reason is FinishReason.STOP
    assert result.usage is not None
    assert result.usage.input_tokens
    assert result.usage.output_tokens


async def test_a_real_model_streams_its_answer() -> None:
    request = ask("Count from one to five in words.", max_output_tokens=60, temperature=0)

    events = [event async for event in gateway().chat_stream(PRINCIPAL, request)]

    text = "".join(event.text for event in events if isinstance(event, TextDelta))
    assert len([event for event in events if isinstance(event, TextDelta)]) > 1
    assert "three" in text.lower()
    assert isinstance(events[-1], FinishEvent)
    assert any(isinstance(event, UsageEvent) for event in events)


async def test_a_real_model_gives_structured_output() -> None:
    schema = {
        "type": "object",
        "properties": {"city": {"type": "string"}, "country": {"type": "string"}},
        "required": ["city", "country"],
    }
    request = ask("Where is the Colosseum?", response_schema=schema, temperature=0)

    result = await gateway().chat(PRINCIPAL, request)

    assert isinstance(result.parsed, dict)
    assert set(result.parsed) == {"city", "country"}
    assert "rom" in result.parsed["city"].lower()


async def test_structured_output_also_works_when_it_has_to_be_emulated() -> None:
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "integer"}},
        "required": ["answer"],
    }
    options = BindingOptions(timeout_s=300, native_structured_output=False)
    request = ask("What is 6 times 7?", response_schema=schema, temperature=0)

    result = await gateway(options=options).chat(PRINCIPAL, request)

    assert result.parsed == {"answer": 42}


async def test_the_prompt_is_not_silently_truncated() -> None:
    """The reason the gateway always asks for a context window: Ollama's own default is small
    and it drops the start of a long prompt without a word, which is where the instructions are."""
    filler = "The weather in the valley was mild and the river was calm. " * 330
    prompt = (
        "The secret word is BANANA42. Remember it.\n\n"
        f"{filler}\n\n"
        "What was the secret word? Reply with the word only."
    )

    result = await gateway().chat(PRINCIPAL, ask(prompt, max_output_tokens=20, temperature=0))

    assert "BANANA42" in result.text.upper()


async def test_a_real_embedding_model_puts_similar_texts_close() -> None:
    inputs = (
        "The cat sits on the mat.",
        "A kitten is resting on a rug.",
        "The quarterly tax report is due on Friday.",
    )

    result = await gateway().embed(PRINCIPAL, EmbeddingRequest(inputs=inputs))

    cat, kitten, tax = result.vectors
    assert result.dimensions == len(cat) > 100
    assert result.space_id.startswith("emb-")
    assert cosine(cat, kitten) > cosine(cat, tax)


async def test_the_whole_service_answers_over_http() -> None:
    authenticator = DevAuthenticator(
        ServiceSettings(_env_file=None, environment=Environment.TEST),
        tenant_id=DEV_TENANT_ID,
        user_id=DEV_USER_ID,
    )
    app = create_app(gateway(), authenticator)
    transport = httpx.ASGITransport(app=app)
    body = {
        "messages": [{"role": "user", "content": "Reply with exactly one word: pong"}],
        "max_output_tokens": 20,
        "temperature": 0,
    }

    async with httpx.AsyncClient(
        transport=transport, base_url="http://gateway", timeout=300
    ) as client:
        whole = await client.post("/v1/chat", json=body)
        streamed = await client.post("/v1/chat", json={**body, "stream": True})
        profile = await client.get("/v1/profiles/chat.main")
        vectors = await client.post("/v1/embeddings", json={"inputs": ["hello"]})

    assert "pong" in whole.json()["text"].lower()
    assert streamed.headers["content-type"].startswith("text/event-stream")
    assert '"type":"finish"' in streamed.text
    assert profile.json()["locality"] == "local"
    assert profile.json()["structured_output"] == "native"
    assert vectors.json()["dimensions"] > 100


async def test_an_unknown_model_is_a_typed_error_and_not_a_hang() -> None:
    missing = make_model_binding(
        tenant_id=DEV_TENANT_ID,
        provider=ProviderKind.OLLAMA,
        model="this-model-does-not-exist:1b",
        base_url=URL,
    )
    unbound = ModelGateway(InMemoryBindingRepository([missing]), EnvSecretResolver())

    with pytest.raises(InvalidRequestError, match="not found"):
        await unbound.chat(PRINCIPAL, ask("hi"))
