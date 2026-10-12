import json
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import TypeAdapter

from factotum.common.config import Environment, ServiceSettings
from factotum.common.devauth import DEVICE_HEADER, SUBJECT_HEADER, DevAuthenticator
from factotum.contracts.chat import ErrorEvent
from factotum.contracts.conversation import Delta, Finished, Started, TurnEvent, TurnEventUnion
from factotum.contracts.errors import ChatNotConfiguredError, GatewayUnavailableError
from factotum.contracts.testing import make_tenant_id, make_user_id
from factotum.conversation.api import _server_sent, create_app
from factotum.conversation.service import ConversationService
from factotum.conversation.testing import Harness, passage

TENANT = make_tenant_id()
EVENTS: TypeAdapter[TurnEvent] = TypeAdapter(TurnEventUnion)


class Rig:
    def __init__(self, *, ready: bool | None = None) -> None:
        self.harness = Harness()
        self.service = ConversationService(
            self.harness.store, self.harness.engine, self.harness.history
        )

        async def check() -> bool:
            return bool(ready)

        self.app = create_app(
            DevAuthenticator(
                ServiceSettings(_env_file=None, environment=Environment.TEST),
                tenant_id=TENANT,
                user_id=make_user_id(),
            ),
            service=self.service,
            ready=None if ready is None else check,
        )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://conversation"
        )


@pytest.fixture
async def client() -> AsyncIterator[tuple[Rig, httpx.AsyncClient]]:
    rig = Rig()
    async with rig.client() as http:
        yield rig, http


def events_of(response: httpx.Response) -> list[TurnEvent]:
    return [
        EVENTS.validate_python(json.loads(line.removeprefix("data: ")))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def said(events: list[TurnEvent]) -> str:
    return "".join(e.text for e in events if isinstance(e, Delta))


async def ask(http: httpx.AsyncClient, message: str, **fields: Any) -> httpx.Response:
    return await http.post("/v1/turns", json={"message": message, **fields})


class TestProbes:
    async def test_it_is_alive_without_credentials(
        self, client: tuple[Rig, httpx.AsyncClient]
    ) -> None:
        _, http = client

        assert (await http.get("/healthz")).json() == {"status": "ok"}

    @pytest.mark.parametrize(("ready", "status"), [(None, 200), (True, 200), (False, 503)])
    async def test_it_is_ready_when_what_it_depends_on_answers(
        self, ready: bool | None, status: int
    ) -> None:
        async with Rig(ready=ready).client() as http:
            assert (await http.get("/readyz")).status_code == status


class TestATurn:
    async def test_the_answer_arrives_as_server_sent_events_in_order(
        self, client: tuple[Rig, httpx.AsyncClient]
    ) -> None:
        rig, http = client
        rig.harness.documents.passages = [passage("Hold the button.")]
        rig.harness.chat.answers.append(["Hold it ", "for ten seconds [1]."])

        response = await ask(http, "how do I reset the router?")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-store"
        events = events_of(response)
        assert next(e.type for e in events) == "started"
        assert [e.type for e in events][-1] == "finished"
        assert said(events) == "Hold it for ten seconds [1]."
        assert "citations" in [e.type for e in events]

    async def test_every_event_is_a_frame_of_its_own(
        self, client: tuple[Rig, httpx.AsyncClient]
    ) -> None:
        _, http = client

        response = await ask(http, "hello")

        frames = [chunk for chunk in response.text.split("\n\n") if chunk]
        assert all(frame.startswith("data: {") for frame in frames)
        assert len(frames) == len(events_of(response))

    async def test_a_conversation_can_be_carried_on_by_its_id(
        self, client: tuple[Rig, httpx.AsyncClient]
    ) -> None:
        _, http = client
        [first] = [e for e in events_of(await ask(http, "one")) if isinstance(e, Started)]

        [again] = [
            e
            for e in events_of(await ask(http, "two", conversation_id=str(first.conversation_id)))
            if isinstance(e, Started)
        ]

        assert again.conversation_id == first.conversation_id

    async def test_a_device_is_a_conversation_of_its_own(
        self, client: tuple[Rig, httpx.AsyncClient]
    ) -> None:
        _, http = client
        [plain] = [e for e in events_of(await ask(http, "one")) if isinstance(e, Started)]

        on_a_speaker = await http.post(
            "/v1/turns", json={"message": "two"}, headers={DEVICE_HEADER: str(uuid4())}
        )

        [other] = [e for e in events_of(on_a_speaker) if isinstance(e, Started)]
        assert other.conversation_id != plain.conversation_id

    async def test_what_goes_wrong_after_the_answer_began_is_its_last_event(
        self, client: tuple[Rig, httpx.AsyncClient]
    ) -> None:
        rig, http = client
        rig.harness.documents.passages = [passage()]
        rig.harness.chat.answers.append(["Ten ", GatewayUnavailableError("down")])

        response = await ask(http, "how long?")

        assert response.status_code == 200
        events = events_of(response)
        assert events[-1].type == "failed"
        assert events[-1].code == "gateway_unavailable"

    async def test_a_failure_the_model_reports_is_a_frame_too(
        self, client: tuple[Rig, httpx.AsyncClient]
    ) -> None:
        rig, http = client
        rig.harness.documents.passages = [passage()]
        rig.harness.chat.answers.append(
            ["Ten ", ErrorEvent(code="provider_timeout", message="slow")]
        )

        events = events_of(await ask(http, "how long?"))

        assert events[-1].type == "failed"


class TestWhatMakesAnAnswerImpossible:
    async def test_no_chat_model_is_an_error_status_and_not_a_stream(
        self, client: tuple[Rig, httpx.AsyncClient]
    ) -> None:
        rig, http = client
        rig.harness.chat.profile_failure = ChatNotConfiguredError("none")

        response = await ask(http, "hello")

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "chat_not_configured"

    async def test_a_conversation_that_is_not_the_callers_is_not_found(
        self, client: tuple[Rig, httpx.AsyncClient]
    ) -> None:
        _, http = client

        response = await ask(http, "hello", conversation_id=str(uuid4()))

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "conversation_not_found"

    async def test_a_message_the_model_cannot_take_is_too_large(
        self, client: tuple[Rig, httpx.AsyncClient]
    ) -> None:
        rig, http = client
        rig.harness.chat.max_input_tokens = 500

        response = await ask(http, "word " * 700)

        assert response.status_code == 413
        assert response.json()["error"]["code"] == "context_too_long"

    @pytest.mark.parametrize("message", ["", "   ", "a" * 4001])
    async def test_a_message_that_breaks_the_contract_is_refused_without_echoing_it(
        self, client: tuple[Rig, httpx.AsyncClient], message: str
    ) -> None:
        _, http = client

        response = await ask(http, message)

        assert response.status_code == 422
        assert message.strip() == "" or message not in response.text

    async def test_a_field_it_does_not_know_is_refused(
        self, client: tuple[Rig, httpx.AsyncClient]
    ) -> None:
        _, http = client

        assert (await ask(http, "hi", history=["earlier"])).status_code == 422

    async def test_somebody_who_is_not_who_they_say_is_not_let_in(
        self, client: tuple[Rig, httpx.AsyncClient]
    ) -> None:
        _, http = client

        response = await http.post(
            "/v1/turns", json={"message": "hi"}, headers={SUBJECT_HEADER: "the-president"}
        )

        assert response.status_code == 401

    async def test_a_body_that_is_too_large_is_refused_before_it_is_read(
        self, client: tuple[Rig, httpx.AsyncClient]
    ) -> None:
        _, http = client

        response = await http.post("/v1/turns", content=b"x" * (65 * 1024))

        assert response.status_code == 413


class TestReadingAndForgettingWhatWasSaid:
    async def test_conversations_are_listed_read_searched_and_forgotten(
        self, client: tuple[Rig, httpx.AsyncClient]
    ) -> None:
        rig, http = client
        [started] = [
            e
            for e in events_of(await ask(http, "how do I reset the router?"))
            if isinstance(e, Started)
        ]
        await rig.harness.history.embed_pending()

        listed = (await http.get("/v1/conversations")).json()
        detail = (await http.get(f"/v1/conversations/{started.conversation_id}")).json()
        found = (await http.post("/v1/history/search", json={"query": "router"})).json()

        assert [c["id"] for c in listed["conversations"]] == [str(started.conversation_id)]
        assert [e["question"] for e in detail["exchanges"]] == ["how do I reset the router?"]
        assert [h["exchange"]["question"] for h in found["hits"]] == ["how do I reset the router?"]

        deleted = await http.delete(f"/v1/conversations/{started.conversation_id}")
        assert deleted.status_code == 204
        assert (await http.get(f"/v1/conversations/{started.conversation_id}")).status_code == 404
        assert (await http.get("/v1/conversations")).json() == {"conversations": []}

    async def test_all_of_it_can_be_forgotten(self, client: tuple[Rig, httpx.AsyncClient]) -> None:
        rig, http = client
        await ask(http, "one")
        rig.harness.clock.advance(hours=2)
        await ask(http, "two")

        response = await http.delete("/v1/history")

        assert response.json() == {"conversations": 2}
        assert (await http.get("/v1/conversations")).json() == {"conversations": []}

    async def test_a_page_of_the_list_can_be_asked_for(
        self, client: tuple[Rig, httpx.AsyncClient]
    ) -> None:
        rig, http = client
        for n in range(3):
            await ask(http, f"q{n}")
            rig.harness.clock.advance(hours=1)

        page = (await http.get("/v1/conversations", params={"limit": 2, "offset": 2})).json()

        assert len(page["conversations"]) == 1

    @pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}])
    async def test_a_page_outside_the_limits_is_refused(
        self, client: tuple[Rig, httpx.AsyncClient], params: dict[str, int]
    ) -> None:
        _, http = client

        assert (await http.get("/v1/conversations", params=params)).status_code == 422

    async def test_a_search_needs_something_to_go_by(
        self, client: tuple[Rig, httpx.AsyncClient]
    ) -> None:
        _, http = client

        assert (await http.post("/v1/history/search", json={})).status_code == 422


class TestASharedDevice:
    async def test_it_can_talk_and_is_no_way_to_read_what_was_said(
        self, client: tuple[Rig, httpx.AsyncClient]
    ) -> None:
        _, http = client
        shared = {SUBJECT_HEADER: "tenant"}

        talked = await http.post("/v1/turns", json={"message": "hello"}, headers=shared)
        listed = await http.get("/v1/conversations", headers=shared)
        forgotten = await http.delete("/v1/history", headers=shared)

        assert talked.status_code == 200
        assert listed.status_code == 403
        assert listed.json()["error"]["code"] == "policy_violation"
        assert forgotten.status_code == 403


class TestTheStream:
    async def test_when_the_caller_goes_away_the_answer_is_closed(self) -> None:
        closed: list[bool] = []

        async def rest() -> AsyncGenerator[TurnEvent]:
            try:
                yield Delta(text="a")
                yield Delta(text="b")
            finally:
                closed.append(True)

        frames = _server_sent(Delta(text="first"), rest())
        assert (await anext(frames)).startswith("data: ")
        assert (await anext(frames)).startswith("data: ")

        await frames.aclose()  # the connection was dropped

        assert closed == [True]

    async def test_the_last_event_of_a_finished_turn_is_finished(
        self, client: tuple[Rig, httpx.AsyncClient]
    ) -> None:
        _, http = client

        events = events_of(await ask(http, "hello"))

        assert isinstance(events[-1], Finished)
