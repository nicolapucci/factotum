import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest
from pydantic import TypeAdapter

from factotum.common.config import Environment, ServiceSettings
from factotum.common.devauth import DEVICE_HEADER, SUBJECT_HEADER, DevAuthenticator
from factotum.contracts.audio import AudioPayload
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
from factotum.contracts.errors import (
    AuthError,
    BindingNotFoundError,
    CapabilityNotSupportedError,
    ContentFilteredError,
    ContextTooLongError,
    FactotumError,
    InvalidRequestError,
    InvalidSchemaError,
    InvalidStructuredOutputError,
    PolicyViolationError,
    ProviderAuthError,
    ProviderError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    RateLimitedError,
    RoleNotSupportedError,
    SecretNotFoundError,
    UrlNotAllowedError,
)
from factotum.contracts.identity import Owner, Subject
from factotum.contracts.models import BindingOptions, ModelBinding, ModelRole, ProviderKind
from factotum.contracts.speech import SynthesisError, SynthesisEvent
from factotum.contracts.testing import make_model_binding, make_tenant_id, make_user_id
from factotum.model_gateway.api import MAX_BODY_BYTES, _server_sent, create_app
from factotum.model_gateway.bindings import InMemoryBindingRepository
from factotum.model_gateway.core import ModelGateway
from factotum.model_gateway.providers.base import EmbeddingBatch, SpeechPiece
from factotum.model_gateway.retry import RetryPolicy
from factotum.model_gateway.secrets import EnvSecretResolver
from factotum.model_gateway.testing import ScriptedAdapter, Sleeps, TrackedClients

TENANT = make_tenant_id()
ALICE = make_user_id()
USER_MODEL = "alices-own-model"
TENANT_MODEL = "the-tenants-model"
SECRET = "sk-live-0123456789abcdefghijklmnop"


def binding(
    role: ModelRole = ModelRole.CHAT_MAIN,
    *,
    user: bool = False,
    model: str = TENANT_MODEL,
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


class Service:
    """The application over an in-process transport, with a scripted adapter behind it."""

    def __init__(self, *bindings: ModelBinding, docs: bool = False) -> None:
        self.adapter = ScriptedAdapter()
        self.clients = TrackedClients()
        gateway = ModelGateway(
            InMemoryBindingRepository(bindings),
            EnvSecretResolver({"KEY": SECRET}),
            adapters=dict.fromkeys(ProviderKind, self.adapter),
            client_factory=self.clients,
            retry=RetryPolicy(attempts=2, base_delay_s=0.0, max_delay_s=0.0),
            sleep=Sleeps(),
            jitter=lambda: 0.0,
        )
        authenticator = DevAuthenticator(
            ServiceSettings(_env_file=None, environment=Environment.TEST),
            tenant_id=TENANT,
            user_id=ALICE,
        )
        self.app = create_app(gateway, authenticator, docs=docs)

    def client(self, *, raise_app_exceptions: bool = True) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=self.app, raise_app_exceptions=raise_app_exceptions)
        return httpx.AsyncClient(transport=transport, base_url="http://gateway")


def chat_body(text: str = "Hello", **fields: Any) -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": text}], **fields}


def clip(data: bytes = b"wav-bytes", sample_rate: int = 16000) -> AudioPayload:
    return AudioPayload(sample_rate=sample_rate, data=data)


def reply(text: str = "Hi!", usage: Usage | None = None) -> ChatResult:
    return ChatResult(text=text, finish_reason=FinishReason.STOP, usage=usage)


def frames(response: httpx.Response) -> list[dict[str, Any]]:
    """The events of a server-sent-events body, as JSON objects."""
    payloads = [
        line.removeprefix("data: ")
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    return [json.loads(payload) for payload in payloads]


class TestProbes:
    @pytest.mark.parametrize("path", ["/healthz", "/readyz"])
    async def test_they_answer_without_credentials_or_bindings(self, path: str) -> None:
        async with Service().client() as client:
            response = await client.get(path)

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestChat:
    async def test_the_answer_comes_back_as_the_normalized_result(self) -> None:
        service = Service(binding())
        service.adapter.will_chat(reply("Hi!", Usage(input_tokens=3, output_tokens=2)))

        async with service.client() as client:
            response = await client.post("/v1/chat", json=chat_body())

        assert response.status_code == 200
        assert response.json() == {
            "text": "Hi!",
            "parsed": None,
            "finish_reason": "stop",
            "usage": {"input_tokens": 3, "output_tokens": 2},
        }

    async def test_the_request_reaches_the_adapter_as_the_contract_says(self) -> None:
        service = Service(binding())
        service.adapter.will_chat(reply())

        async with service.client() as client:
            await client.post(
                "/v1/chat", json=chat_body("What?", max_output_tokens=50, temperature=0.2)
            )

        sent = service.adapter.chats[0][1]
        assert sent.messages[-1].content == "What?"
        assert sent.max_output_tokens == 50
        assert sent.temperature == 0.2

    async def test_a_user_is_served_by_their_own_binding_by_default(self) -> None:
        service = Service(binding(), binding(user=True, model=USER_MODEL))
        service.adapter.will_chat(reply())

        async with service.client() as client:
            await client.post("/v1/chat", json=chat_body())

        assert service.adapter.chats[0][0].model == USER_MODEL

    async def test_a_device_that_acts_for_the_tenant_is_served_by_the_tenants_binding(self) -> None:
        service = Service(binding(), binding(user=True, model=USER_MODEL))
        service.adapter.will_chat(reply())

        async with service.client() as client:
            await client.post("/v1/chat", json=chat_body(), headers={SUBJECT_HEADER: "tenant"})

        assert service.adapter.chats[0][0].model == TENANT_MODEL

    async def test_a_structured_answer_comes_back_parsed(self) -> None:
        service = Service(binding())
        service.adapter.will_chat(reply('{"city": "Rome"}'))
        schema = {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        }

        async with service.client() as client:
            response = await client.post("/v1/chat", json=chat_body(response_schema=schema))

        assert response.json()["parsed"] == {"city": "Rome"}

    async def test_a_request_that_still_classifies_its_data_is_refused_and_not_ignored(
        self,
    ) -> None:
        # Where the data goes is decided by the deployment and the providers the user bound
        # (ADR 0010). A caller that still asks for a policy must hear that nothing enforces it.
        service = Service(binding())

        async with service.client() as client:
            response = await client.post(
                "/v1/chat", json=chat_body(data_classification="local_only")
            )

        assert response.status_code == 422
        assert response.json()["error"]["details"]["errors"][0]["loc"] == [
            "body",
            "data_classification",
        ]
        assert service.adapter.chats == []


class TestAuthentication:
    async def test_a_request_the_authenticator_refuses_is_a_401(self) -> None:
        service = Service(binding())

        async with service.client() as client:
            response = await client.post(
                "/v1/chat", json=chat_body(), headers={SUBJECT_HEADER: "somebody-else"}
            )

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "auth_error"
        assert service.adapter.chats == []

    async def test_a_device_header_that_is_not_an_id_is_a_401(self) -> None:
        async with Service(binding()).client() as client:
            response = await client.post(
                "/v1/chat", json=chat_body(), headers={DEVICE_HEADER: "not-an-id"}
            )

        assert response.status_code == 401

    async def test_authentication_comes_before_the_body_is_looked_at(self) -> None:
        async with Service(binding()).client() as client:
            response = await client.post(
                "/v1/chat", json={"nonsense": True}, headers={SUBJECT_HEADER: "somebody-else"}
            )

        assert response.status_code == 401

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("post", "/v1/chat"),
            ("post", "/v1/embeddings"),
            ("post", "/v1/transcribe"),
            ("post", "/v1/synthesize"),
            ("get", "/v1/profiles/chat.main"),
        ],
    )
    async def test_every_route_needs_it(self, method: str, path: str) -> None:
        async with Service(binding()).client() as client:
            response = await client.request(
                method,
                path,
                json=chat_body() if method == "post" else None,
                headers={SUBJECT_HEADER: "somebody-else"},
            )

        assert response.status_code == 401


class TestInvalidRequests:
    async def test_a_body_that_breaks_the_contract_is_a_422_that_says_where(self) -> None:
        async with Service(binding()).client() as client:
            response = await client.post("/v1/chat", json={"messages": []})

        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "invalid_request_body"
        assert error["details"]["errors"][0]["loc"] == ["body", "messages"]

    async def test_what_was_sent_is_never_echoed_back(self) -> None:
        body = chat_body(f"my key is {SECRET}")
        body["messages"][0]["colour"] = "blue"  # an unknown field makes the body invalid

        async with Service(binding()).client() as client:
            response = await client.post("/v1/chat", json=body)

        assert response.status_code == 422
        assert SECRET not in response.text
        assert "my key" not in response.text

    async def test_an_unknown_field_is_refused(self) -> None:
        async with Service(binding()).client() as client:
            response = await client.post("/v1/chat", json=chat_body(model="gpt-5"))

        assert response.status_code == 422

    async def test_a_conversation_that_does_not_end_with_the_user_is_refused(self) -> None:
        body = {"messages": [{"role": "assistant", "content": "Hi"}]}

        async with Service(binding()).client() as client:
            response = await client.post("/v1/chat", json=body)

        assert response.status_code == 422
        assert "last message must be a user message" in response.text

    async def test_a_body_that_is_not_json_is_a_422(self) -> None:
        async with Service(binding()).client() as client:
            response = await client.post(
                "/v1/chat", content=b"{not json", headers={"content-type": "application/json"}
            )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request_body"

    async def test_a_role_that_does_not_exist_is_a_422(self) -> None:
        async with Service(binding()).client() as client:
            response = await client.get("/v1/profiles/telepathy")

        assert response.status_code == 422

    async def test_a_body_that_declares_itself_huge_is_refused_before_it_is_read(self) -> None:
        async with Service(binding()).client() as client:
            response = await client.post(
                "/v1/chat",
                content=b"{}",
                headers={
                    "content-type": "application/json",
                    "content-length": str(MAX_BODY_BYTES + 1),
                },
            )

        assert response.status_code == 413
        assert response.json()["error"]["code"] == "payload_too_large"


class Upstream:
    """A provider stream that knows whether it was closed."""

    def __init__(self) -> None:
        self.closed = False
        self.events = self._events()

    async def _events(self) -> AsyncGenerator[EventUnion]:
        try:
            yield TextDelta(text="a")
            yield TextDelta(text="b")
        finally:
            self.closed = True


class TestAClientThatGoesAway:
    """A stream nobody reads any more must stop: it costs tokens."""

    async def test_closing_the_response_body_closes_the_provider_stream(self) -> None:
        upstream = Upstream()
        body = _server_sent(await anext(upstream.events), upstream.events)

        first = await anext(body)
        await body.aclose()

        assert first == 'data: {"type":"text_delta","text":"a"}\n\n'
        assert upstream.closed

    async def test_reading_it_to_the_end_closes_it_too(self) -> None:
        upstream = Upstream()

        chunks = [
            chunk async for chunk in _server_sent(await anext(upstream.events), upstream.events)
        ]

        assert len(chunks) == 2
        assert upstream.closed


class TestStreaming:
    async def test_the_events_arrive_as_server_sent_events_in_order(self) -> None:
        service = Service(binding())
        service.adapter.will_stream(
            TextDelta(text="Hel"),
            TextDelta(text="lo"),
            UsageEvent(usage=Usage(input_tokens=3, output_tokens=2)),
            FinishEvent(reason=FinishReason.STOP),
        )

        async with service.client() as client:
            response = await client.post("/v1/chat", json=chat_body(stream=True))

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-store"
        assert frames(response) == [
            {"type": "text_delta", "text": "Hel"},
            {"type": "text_delta", "text": "lo"},
            {"type": "usage", "usage": {"input_tokens": 3, "output_tokens": 2}},
            {"type": "finish", "reason": "stop"},
        ]

    async def test_the_spaces_between_the_chunks_of_an_answer_arrive_with_them(self) -> None:
        service = Service(binding())
        service.adapter.will_stream(
            TextDelta(text="Hold"),
            TextDelta(text=" the reset"),
            TextDelta(text=" button "),
            TextDelta(text="now."),
            FinishEvent(reason=FinishReason.STOP),
        )

        async with service.client() as client:
            response = await client.post("/v1/chat", json=chat_body(stream=True))

        said = "".join(f["text"] for f in frames(response) if f["type"] == "text_delta")
        assert said == "Hold the reset button now."

    async def test_every_event_is_a_frame_of_its_own(self) -> None:
        service = Service(binding())
        service.adapter.will_stream(TextDelta(text="a"), FinishEvent(reason=FinishReason.STOP))

        async with service.client() as client:
            response = await client.post("/v1/chat", json=chat_body(stream=True))

        assert response.text == (
            'data: {"type":"text_delta","text":"a"}\n\ndata: {"type":"finish","reason":"stop"}\n\n'
        )

    async def test_a_failure_to_start_is_an_error_status_and_not_a_stream(self) -> None:
        service = Service(binding())
        service.adapter.will_stream(ProviderAuthError("the key was refused"))

        async with service.client() as client:
            response = await client.post("/v1/chat", json=chat_body(stream=True))

        assert response.status_code == 502
        assert response.headers["content-type"] == "application/json"
        assert response.json()["error"]["code"] == "provider_auth_error"

    async def test_no_binding_is_a_404_even_when_a_stream_was_asked_for(self) -> None:
        async with Service().client() as client:
            response = await client.post("/v1/chat", json=chat_body(stream=True))

        assert response.status_code == 404

    async def test_a_failure_after_the_start_is_the_last_event_of_the_stream(self) -> None:
        service = Service(binding())
        service.adapter.will_stream(TextDelta(text="Hel"), ProviderUnavailableError("it crashed"))

        async with service.client() as client:
            response = await client.post("/v1/chat", json=chat_body(stream=True))

        assert response.status_code == 200
        assert frames(response) == [
            {"type": "text_delta", "text": "Hel"},
            {"type": "error", "code": "provider_unavailable", "message": "it crashed"},
        ]

    async def test_an_answer_with_no_events_is_an_empty_stream(self) -> None:
        service = Service(binding())
        service.adapter.will_stream()

        async with service.client() as client:
            response = await client.post("/v1/chat", json=chat_body(stream=True))

        assert response.status_code == 200
        assert response.text == ""

    async def test_the_provider_stream_is_closed_and_so_is_the_client(self) -> None:
        service = Service(binding())
        service.adapter.will_stream(TextDelta(text="a"), FinishEvent(reason=FinishReason.STOP))

        async with service.client() as client:
            await client.post("/v1/chat", json=chat_body(stream=True))

        assert service.adapter.stream_endings == ["completed"]
        assert all(c.is_closed for c in service.clients.created)

    async def test_structured_output_cannot_be_streamed(self) -> None:
        schema = {"type": "object"}

        async with Service(binding()).client() as client:
            response = await client.post(
                "/v1/chat", json=chat_body(stream=True, response_schema=schema)
            )

        assert response.status_code == 422
        assert "cannot be streamed" in response.text

    async def test_a_stream_whose_events_carry_an_error_event_of_the_contract_is_valid_json(
        self,
    ) -> None:
        service = Service(binding())
        service.adapter.will_stream(TextDelta(text="x"), RateLimitedError("slow down"))

        async with service.client() as client:
            response = await client.post("/v1/chat", json=chat_body(stream=True))

        assert ErrorEvent.model_validate(frames(response)[-1]).code == "rate_limited"


class TestEmbeddings:
    async def test_vectors_come_back_with_their_space(self) -> None:
        service = Service(binding(ModelRole.EMBEDDING, model="nomic"))
        service.adapter.will_embed(
            EmbeddingBatch(vectors=((0.1, 0.2), (0.3, 0.4)), usage=Usage(input_tokens=6))
        )

        async with service.client() as client:
            response = await client.post("/v1/embeddings", json={"inputs": ["a", "b"]})

        body = response.json()
        assert response.status_code == 200
        assert body["vectors"] == [[0.1, 0.2], [0.3, 0.4]]
        assert body["dimensions"] == 2
        assert body["space_id"].startswith("emb-")
        assert body["usage"] == {"input_tokens": 6, "output_tokens": None}

    async def test_no_inputs_is_a_422(self) -> None:
        async with Service(binding(ModelRole.EMBEDDING)).client() as client:
            response = await client.post("/v1/embeddings", json={"inputs": []})

        assert response.status_code == 422

    async def test_blank_inputs_are_a_422(self) -> None:
        async with Service(binding(ModelRole.EMBEDDING)).client() as client:
            response = await client.post("/v1/embeddings", json={"inputs": ["a", "  "]})

        assert response.status_code == 422

    async def test_no_embedding_binding_is_a_404(self) -> None:
        async with Service(binding()).client() as client:
            response = await client.post("/v1/embeddings", json={"inputs": ["a"]})

        assert response.status_code == 404
        assert response.json()["error"]["details"] == {"role": "embedding"}


class TestTranscription:
    async def test_what_the_adapter_heard_comes_back(self) -> None:
        service = Service(binding(ModelRole.STT, model="base"))
        service.adapter.will_transcribe("how do I reset the router?")

        async with service.client() as client:
            response = await client.post(
                "/v1/transcribe", json={"audio": clip().model_dump(mode="json")}
            )

        assert response.status_code == 200
        assert response.json() == {"text": "how do I reset the router?"}

    async def test_the_recording_reaches_the_adapter_byte_for_byte(self) -> None:
        service = Service(binding(ModelRole.STT, model="base"))
        service.adapter.will_transcribe("hi")
        recording = clip(data=bytes(range(200)))

        async with service.client() as client:
            await client.post("/v1/transcribe", json={"audio": recording.model_dump(mode="json")})

        assert service.adapter.transcriptions[0][1] == recording

    async def test_no_stt_binding_is_a_404(self) -> None:
        async with Service(binding()).client() as client:
            response = await client.post(
                "/v1/transcribe", json={"audio": clip().model_dump(mode="json")}
            )

        assert response.status_code == 404
        assert response.json()["error"]["details"] == {"role": "stt"}


class TestSynthesis:
    async def test_what_the_adapter_made_comes_back(self) -> None:
        service = Service(binding(ModelRole.TTS, model="it_IT-paola-medium"))
        service.adapter.will_synthesize(clip(data=b"spoken-aloud"))

        async with service.client() as client:
            response = await client.post("/v1/synthesize", json={"text": "Dinner is ready"})

        assert response.status_code == 200
        assert AudioPayload.model_validate(response.json()["audio"]).data == b"spoken-aloud"

    async def test_the_text_reaches_the_adapter_as_it_was(self) -> None:
        service = Service(binding(ModelRole.TTS, model="it_IT-paola-medium"))
        service.adapter.will_synthesize(clip())

        async with service.client() as client:
            await client.post("/v1/synthesize", json={"text": "Dinner is ready"})

        assert service.adapter.syntheses[0][1] == "Dinner is ready"

    async def test_blank_text_is_a_422(self) -> None:
        async with Service(binding(ModelRole.TTS)).client() as client:
            response = await client.post("/v1/synthesize", json={"text": ""})

        assert response.status_code == 422

    async def test_no_tts_binding_is_a_404(self) -> None:
        async with Service(binding()).client() as client:
            response = await client.post("/v1/synthesize", json={"text": "hello"})

        assert response.status_code == 404
        assert response.json()["error"]["details"] == {"role": "tts"}


def a_voice(samples: int = 100, rate: int = 22050) -> SpeechPiece:
    return SpeechPiece(sample_rate=rate, pcm=bytes(i % 251 for i in range(2 * samples)))


_VOICE_EVENT: TypeAdapter[Any] = TypeAdapter(SynthesisEvent)


class TestAStreamedVoice:
    async def test_the_voice_arrives_as_server_sent_events_in_order(self) -> None:
        service = Service(binding(ModelRole.TTS, model="it_IT-paola-medium"))
        service.adapter.will_voice(a_voice(100))

        async with service.client() as client:
            response = await client.post(
                "/v1/synthesize", json={"text": "Dinner is ready", "stream": True}
            )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-store"
        assert [frame["type"] for frame in frames(response)] == ["format", "samples", "done"]
        assert frames(response)[0] == {
            "type": "format",
            "format": "pcm_s16le",
            "sample_rate": 22050,
        }

    async def test_what_the_events_carry_is_what_the_voice_said(self) -> None:
        service = Service(binding(ModelRole.TTS, model="it_IT-paola-medium"))
        voice = a_voice(2500)  # more than a chunk
        service.adapter.will_voice(voice)

        async with service.client() as client:
            response = await client.post(
                "/v1/synthesize", json={"text": "Dinner is ready", "stream": True}
            )

        events = [_VOICE_EVENT.validate_python(frame) for frame in frames(response)]
        said = b"".join(event.data for event in events if event.type == "samples")
        assert said == voice.pcm
        assert sum(event.type == "samples" for event in events) == 2

    async def test_every_event_is_a_frame_of_its_own(self) -> None:
        service = Service(binding(ModelRole.TTS, model="it_IT-paola-medium"))
        service.adapter.will_voice(SpeechPiece(sample_rate=16000, pcm=b"\x00\x01\x02\x03"))

        async with service.client() as client:
            response = await client.post("/v1/synthesize", json={"text": "hi", "stream": True})

        assert response.text == (
            'data: {"type":"format","format":"pcm_s16le","sample_rate":16000}\n\n'
            'data: {"type":"samples","data":"AAECAw=="}\n\n'
            'data: {"type":"done"}\n\n'
        )

    async def test_the_text_reaches_the_adapter_as_it_was(self) -> None:
        service = Service(binding(ModelRole.TTS, model="it_IT-paola-medium"))
        service.adapter.will_voice(a_voice(10))

        async with service.client() as client:
            await client.post("/v1/synthesize", json={"text": "Dinner is ready", "stream": True})

        assert service.adapter.voices[0][1] == "Dinner is ready"
        assert service.adapter.syntheses == []

    async def test_a_request_that_does_not_ask_for_a_stream_is_still_answered_with_a_clip(
        self,
    ) -> None:
        service = Service(binding(ModelRole.TTS, model="it_IT-paola-medium"))
        service.adapter.will_synthesize(clip(data=b"spoken-aloud"))

        async with service.client() as client:
            response = await client.post(
                "/v1/synthesize", json={"text": "Dinner is ready", "stream": False}
            )

        assert response.headers["content-type"] == "application/json"
        assert AudioPayload.model_validate(response.json()["audio"]).data == b"spoken-aloud"
        assert service.adapter.voices == []

    async def test_a_failure_to_start_is_an_error_status_and_not_a_stream(self) -> None:
        service = Service(binding(ModelRole.TTS, model="it_IT-paola-medium"))
        service.adapter.will_voice(ProviderAuthError("the key was refused"))

        async with service.client() as client:
            response = await client.post("/v1/synthesize", json={"text": "hi", "stream": True})

        assert response.status_code == 502
        assert response.headers["content-type"] == "application/json"
        assert response.json()["error"]["code"] == "provider_auth_error"

    async def test_no_tts_binding_is_a_404_even_when_a_stream_was_asked_for(self) -> None:
        async with Service(binding()).client() as client:
            response = await client.post("/v1/synthesize", json={"text": "hi", "stream": True})

        assert response.status_code == 404
        assert response.json()["error"]["details"] == {"role": "tts"}

    async def test_a_failure_after_the_start_is_the_last_event_of_the_stream(self) -> None:
        service = Service(binding(ModelRole.TTS, model="it_IT-paola-medium"))
        service.adapter.will_voice(a_voice(10), ProviderUnavailableError("it crashed"))

        async with service.client() as client:
            response = await client.post("/v1/synthesize", json={"text": "hi", "stream": True})

        assert response.status_code == 200
        last = _VOICE_EVENT.validate_python(frames(response)[-1])
        assert last == SynthesisError(code="provider_unavailable", message="it crashed")

    async def test_a_voice_with_nothing_to_say_is_done_and_nothing_else(self) -> None:
        service = Service(binding(ModelRole.TTS, model="it_IT-paola-medium"))
        service.adapter.will_voice()

        async with service.client() as client:
            response = await client.post("/v1/synthesize", json={"text": "...", "stream": True})

        assert frames(response) == [{"type": "done"}]

    async def test_the_providers_voice_is_closed_and_so_is_the_client(self) -> None:
        service = Service(binding(ModelRole.TTS, model="it_IT-paola-medium"))
        service.adapter.will_voice(a_voice(10))

        async with service.client() as client:
            await client.post("/v1/synthesize", json={"text": "hi", "stream": True})

        assert service.adapter.voice_endings == ["completed"]
        assert all(c.is_closed for c in service.clients.created)

    async def test_blank_text_is_a_422_whether_or_not_a_stream_was_asked_for(self) -> None:
        async with Service(binding(ModelRole.TTS)).client() as client:
            response = await client.post("/v1/synthesize", json={"text": "", "stream": True})

        assert response.status_code == 422


class TestProfiles:
    async def test_a_profile_says_what_the_role_can_do_and_not_which_model_does_it(self) -> None:
        options = BindingOptions(max_input_tokens=32_000, max_output_tokens=2048)
        service = Service(binding(model="do-not-reveal-this", options=options))

        async with service.client() as client:
            response = await client.get("/v1/profiles/chat.main")

        assert response.status_code == 200
        assert response.json() == {
            "role": "chat.main",
            "max_input_tokens": 32_000,
            "max_output_tokens": 2048,
            "streaming": True,
            "structured_output": "native",
        }
        assert "do-not-reveal-this" not in response.text

    async def test_the_profile_is_the_one_of_the_binding_that_serves_the_caller(self) -> None:
        small = BindingOptions(max_input_tokens=2048)
        service = Service(binding(), binding(user=True, options=small))

        async with service.client() as client:
            mine = await client.get("/v1/profiles/chat.main")
            shared = await client.get("/v1/profiles/chat.main", headers={SUBJECT_HEADER: "tenant"})

        assert mine.json()["max_input_tokens"] == 2048
        assert shared.json()["max_input_tokens"] == 8192

    async def test_an_unbound_role_is_a_404(self) -> None:
        async with Service().client() as client:
            response = await client.get("/v1/profiles/chat.main")

        assert response.status_code == 404


class TestErrorMapping:
    """The status says whose fault it is; the code says exactly what happened."""

    @pytest.mark.parametrize(
        ("error", "status", "code"),
        [
            (AuthError("no"), 401, "auth_error"),
            (PolicyViolationError("no"), 403, "policy_violation"),
            (BindingNotFoundError("no"), 404, "binding_not_found"),
            (ContextTooLongError("too long"), 413, "context_too_long"),
            (InvalidSchemaError("bad"), 422, "invalid_schema"),
            (InvalidRequestError("bad"), 422, "invalid_request"),
            (RoleNotSupportedError("no"), 422, "role_not_supported"),
            (ContentFilteredError("no"), 422, "content_filtered"),
            (RateLimitedError("slow"), 429, "rate_limited"),
            (ProviderTimeoutError("late"), 504, "provider_timeout"),
            (ProviderAuthError("key"), 502, "provider_auth_error"),
            (ProviderUnavailableError("down"), 502, "provider_unavailable"),
            (InvalidStructuredOutputError("bad"), 502, "invalid_structured_output"),
            (SecretNotFoundError("no"), 502, "secret_not_found"),
            (UrlNotAllowedError("no"), 502, "url_not_allowed"),
            (ProviderError("other"), 502, "provider_error"),
            (CapabilityNotSupportedError("odd"), 500, "capability_not_supported"),
            (FactotumError("odd"), 500, "factotum_error"),
        ],
    )
    async def test_each_error_has_its_status_and_keeps_its_code(
        self, error: FactotumError, status: int, code: str
    ) -> None:
        service = Service(binding())
        # Every error is retryable or not on its own account; two attempts cover both.
        service.adapter.will_chat(error, error)

        async with service.client() as client:
            response = await client.post("/v1/chat", json=chat_body())

        assert response.status_code == status
        assert response.json()["error"]["code"] == code

    async def test_the_message_and_the_details_are_passed_on(self) -> None:
        service = Service(binding())
        error = InvalidRequestError("ollama: model 'x' not found", details={"status": 404})
        service.adapter.will_chat(error)

        async with service.client() as client:
            response = await client.post("/v1/chat", json=chat_body())

        assert response.json()["error"] == {
            "code": "invalid_request",
            "message": "ollama: model 'x' not found",
            "details": {"status": 404},
        }

    async def test_a_rate_limit_tells_the_caller_when_to_come_back(self) -> None:
        service = Service(binding())
        service.adapter.will_chat(RateLimitedError("slow down", retry_after_s=3.2))

        async with service.client() as client:
            response = await client.post("/v1/chat", json=chat_body())

        assert response.status_code == 429
        assert response.headers["retry-after"] == "4"

    async def test_a_rate_limit_without_a_hint_has_no_retry_after(self) -> None:
        service = Service(binding())
        service.adapter.will_chat(RateLimitedError("slow down"), RateLimitedError("slow down"))

        async with service.client() as client:
            response = await client.post("/v1/chat", json=chat_body())

        assert "retry-after" not in response.headers

    async def test_an_error_that_nobody_expected_is_a_500_that_explains_nothing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        service = Service(binding())
        service.adapter.will_chat(RuntimeError(f"boom with {SECRET} inside"))

        with caplog.at_level(logging.ERROR, logger="factotum.model_gateway"):
            async with service.client(raise_app_exceptions=False) as client:
                response = await client.post("/v1/chat", json=chat_body())

        assert response.status_code == 500
        assert response.json() == {
            "error": {"code": "internal_error", "message": "internal error", "details": {}}
        }
        assert SECRET not in response.text
        assert any("unhandled error" in record.getMessage() for record in caplog.records)


class TestDocumentation:
    async def test_the_docs_are_there_in_development(self) -> None:
        async with Service(docs=True).client() as client:
            page = await client.get("/docs")
            schema = await client.get("/openapi.json")

        assert page.status_code == 200
        assert "/v1/chat" in schema.json()["paths"]

    async def test_the_docs_are_not_served_otherwise(self) -> None:
        async with Service(docs=False).client() as client:
            responses = [await client.get(path) for path in ("/docs", "/redoc", "/openapi.json")]

        assert [response.status_code for response in responses] == [404, 404, 404]
