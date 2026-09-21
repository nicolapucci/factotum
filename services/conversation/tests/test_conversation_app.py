"""The service put together from its settings: what it refuses to start without, and, against a
real database and a gateway and knowledge service that answer, that it comes up, answers a turn,
remembers it, and finds it again when a message refers to it."""

import json
from typing import Any

import httpx
import pytest
import respx
from pydantic import AnyHttpUrl

from factotum.common.config import Environment
from factotum.contracts.actions import ActOutcome, ActStatus
from factotum.contracts.chat import (
    ChatMessage,
    EventUnion,
    FinishEvent,
    FinishReason,
    TextDelta,
    Usage,
    UsageEvent,
)
from factotum.contracts.errors import AuthError
from factotum.contracts.knowledge import SearchResult
from factotum.conversation.app import build_app
from factotum.conversation.settings import ConversationSettings
from factotum.conversation.testing import WordEmbedder, passage
from factotum.db.settings import DatabaseSettings

GATEWAY = "http://gateway.test"
KNOWLEDGE = "http://knowledge.test"
ACTIONS = "http://actions.test"


def settings_for(database: DatabaseSettings, **values: object) -> ConversationSettings:
    return ConversationSettings(
        _env_file=None,
        environment=Environment.TEST,
        postgres_host=database.postgres_host,
        postgres_port=database.postgres_port,
        postgres_user=database.postgres_user,
        postgres_password=database.postgres_password,
        postgres_db=database.postgres_db,
        conversation_gateway_url=AnyHttpUrl(GATEWAY),
        conversation_knowledge_url=AnyHttpUrl(KNOWLEDGE),
        conversation_keeper_seconds=0.05,
        **values,  # type: ignore[arg-type]
    )


class TestStartingUp:
    def test_it_does_not_start_in_production(self) -> None:
        with pytest.raises(AuthError, match="disabled"):
            build_app(ConversationSettings(_env_file=None, environment=Environment.PRODUCTION))

    @pytest.mark.parametrize("environment", [Environment.DEVELOPMENT, Environment.TEST])
    def test_it_starts_in_development_and_in_tests(self, environment: Environment) -> None:
        app = build_app(ConversationSettings(_env_file=None, environment=environment))

        assert {"/v1/turns", "/healthz", "/readyz"} <= {
            route.path  # type: ignore[attr-defined]
            for route in app.routes
        }

    def test_the_documentation_is_there_for_developers(self) -> None:
        app = build_app(ConversationSettings(_env_file=None, environment=Environment.DEVELOPMENT))

        assert app.docs_url == "/docs"

    async def test_without_a_database_it_does_not_come_up(self) -> None:
        app = build_app(
            ConversationSettings(
                _env_file=None,
                environment=Environment.TEST,
                postgres_port=1,
                postgres_password="x",  # type: ignore[arg-type]
            )
        )

        with pytest.raises(OSError):  # noqa: PT011 - what is refused is the connection
            async with app.router.lifespan_context(app):
                pass  # pragma: no cover - the database is not there


def sse(*events: EventUnion) -> bytes:
    return b"".join(f"data: {event.model_dump_json()}\n\n".encode() for event in events)


class Model:
    """The gateway, as far as this service can tell: it decides, answers, and embeds."""

    def __init__(self) -> None:
        self.answers: list[list[str]] = []
        self.decisions: list[dict[str, str]] = []
        self.streams: list[list[ChatMessage]] = []
        self.embedder = WordEmbedder()

    def chat(self, request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content)
        if body.get("response_schema") is not None:
            said = self.decisions.pop(0) if self.decisions else {"kind": "none"}
            decision = {"reason": "as told", **said}  # a model says why before it says which
            return httpx.Response(
                200,
                json={
                    "text": json.dumps(decision),
                    "parsed": decision,
                    "finish_reason": "stop",
                },
            )
        self.streams.append([ChatMessage.model_validate(m) for m in body["messages"]])
        chunks = self.answers.pop(0) if self.answers else ["An answer."]
        events: list[EventUnion] = [TextDelta(text=c) for c in chunks]
        events += [
            UsageEvent(usage=Usage(input_tokens=10, output_tokens=5)),
            FinishEvent(reason=FinishReason.STOP),
        ]
        return httpx.Response(200, content=sse(*events))

    def embeddings(self, request: httpx.Request) -> httpx.Response:
        texts = json.loads(request.content)["inputs"]
        vectors = [list(self.embedder.vector(t)) for t in texts]
        return httpx.Response(
            200,
            json={
                "vectors": vectors,
                "dimensions": self.embedder.dimensions,
                "space_id": self.embedder.space_id,
            },
        )


def frames(response: httpx.Response) -> list[dict[str, Any]]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="module")
class TestAgainstARealDatabase:
    @respx.mock
    async def test_it_answers_a_turn_remembers_it_and_finds_it_when_a_message_refers_to_it(
        self, database_settings: DatabaseSettings
    ) -> None:
        model = Model()
        respx.get(f"{GATEWAY}/v1/profiles/chat.main").respond(
            200,
            json={
                "role": "chat.main",
                "max_input_tokens": 8192,
                "max_output_tokens": 512,
                "streaming": True,
                "structured_output": "native",
            },
        )
        respx.post(f"{GATEWAY}/v1/chat").mock(side_effect=model.chat)
        respx.post(f"{GATEWAY}/v1/embeddings").mock(side_effect=model.embeddings)
        found = SearchResult(passages=(passage("Hold the button for ten seconds."),))
        respx.post(f"{KNOWLEDGE}/v1/search").respond(200, json=found.model_dump(mode="json"))
        app = build_app(settings_for(database_settings))

        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://conversation"
            ) as http,
        ):
            assert (await http.get("/readyz")).status_code == 200

            # A first message: no past, so the small model is not asked, and the documents answer.
            model.answers.append(["Hold the button ", "for ten seconds [1]."])
            first = await http.post("/v1/turns", json={"message": "how do I reset the router?"})
            assert first.status_code == 200
            events = frames(first)
            assert next(e["type"] for e in events) == "started"
            assert [e["type"] for e in events][-1] == "finished"
            assert "".join(e["text"] for e in events if e["type"] == "delta") == (
                "Hold the button for ten seconds [1]."
            )
            assert events[-1]["untrusted_content"] is True
            assert not any(
                json.loads(call.request.content).get("response_schema") is not None
                for call in respx.calls
                if call.request.url.path == "/v1/chat"
            )

            # A message that carries on the exchange just before: the small model says so.
            model.decisions.append({"kind": "continuation"})
            model.answers.append(["Tieni premuto il pulsante."])
            second = await http.post("/v1/turns", json={"message": "and in Italian?"})
            given = "\n".join(m.content for m in model.streams[-1] if m.role.value == "user")
            assert "You: how do I reset the router?" in given
            assert frames(second)[-1]["reference"] == "continuation"

            # What was said is there to read, and the vectors are made in the background.
            listed = (await http.get("/v1/conversations")).json()
            [conversation] = listed["conversations"]
            assert conversation["exchanges"] == 2
            found_by_words = (
                await http.post("/v1/history/search", json={"query": "router"})
            ).json()
            assert [h["exchange"]["question"] for h in found_by_words["hits"]] == [
                "how do I reset the router?"
            ]

            # A message that points to something earlier: the log is searched.
            model.decisions.append({"kind": "earlier", "topic": "router"})
            model.answers.append(["I told you to hold the button."])
            third = await http.post(
                "/v1/turns", json={"message": "what did you tell me about the router?"}
            )
            given = "\n".join(m.content for m in model.streams[-1] if m.role.value == "user")
            assert "Hold the button" in given
            assert frames(third)[-1]["reference"] == "earlier"

            # And it can all be forgotten.
            assert (await http.delete("/v1/history")).json() == {"conversations": 1}
            assert (await http.get("/v1/conversations")).json() == {"conversations": []}

    @respx.mock
    async def test_a_message_that_asks_for_something_is_answered_by_the_actions_service_and_kept(
        self, database_settings: DatabaseSettings
    ) -> None:
        model = Model()
        respx.get(f"{GATEWAY}/v1/profiles/chat.main").respond(
            200,
            json={
                "role": "chat.main",
                "max_input_tokens": 8192,
                "max_output_tokens": 512,
                "streaming": True,
                "structured_output": "native",
            },
        )
        respx.post(f"{GATEWAY}/v1/chat").mock(side_effect=model.chat)
        respx.post(f"{GATEWAY}/v1/embeddings").mock(side_effect=model.embeddings)
        respx.post(f"{KNOWLEDGE}/v1/search").respond(200, json={"passages": []})
        done = ActOutcome(status=ActStatus.DONE, reply="The lamp is on.", skill="lamp")
        acts = respx.post(f"{ACTIONS}/v1/act").mock(
            side_effect=[
                httpx.Response(200, json=done.model_dump(mode="json")),
                httpx.Response(200, json=ActOutcome.no_action().model_dump(mode="json")),
                httpx.ConnectError("refused"),
            ]
        )
        app = build_app(
            settings_for(database_settings, conversation_actions_url=AnyHttpUrl(ACTIONS))
        )

        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://conversation"
            ) as http,
        ):
            # An action: the actions service answers, and nothing else is asked of anybody.
            first = await http.post("/v1/turns", json={"message": "turn on the lamp"})
            events = frames(first)
            assert [e["type"] for e in events] == [
                "started",
                "status",
                "status",
                "delta",
                "finished",
            ]
            assert [e["phase"] for e in events if e["type"] == "status"] == [
                "understanding",
                "acting",
            ]
            assert "".join(e["text"] for e in events if e["type"] == "delta") == "The lamp is on."
            assert model.streams == []
            sent = acts.calls[0].request
            assert json.loads(sent.content)["message"] == "turn on the lamp"
            assert sent.headers["x-factotum-dev-subject"] == "user"

            # A question: the actions service says so, and it is answered as ever.
            model.answers.append(["It is a device that routes traffic."])
            second = await http.post("/v1/turns", json={"message": "what is a router?"})
            assert [e["type"] for e in frames(second)][-1] == "finished"
            assert len(model.streams) == 1

            # The actions service is away: the message is a question, and the turn says so.
            model.answers.append(["I do not know."])
            third = await http.post("/v1/turns", json={"message": "turn off the lamp"})
            events = frames(third)
            assert [e["code"] for e in events if e["type"] == "warning"] == ["actions_unavailable"]
            assert [e["type"] for e in events][-1] == "finished"

            # What was done is in the log like anything said.
            [conversation] = (await http.get("/v1/conversations")).json()["conversations"]
            assert conversation["exchanges"] == 3
            found = (await http.post("/v1/history/search", json={"query": "lamp"})).json()
            assert "turn on the lamp" in [h["exchange"]["question"] for h in found["hits"]]
