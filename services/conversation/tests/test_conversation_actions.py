import json
from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
import respx

from factotum.common.config import Environment, ServiceSettings
from factotum.common.devauth import DEVICE_HEADER, SUBJECT_HEADER, DevPrincipalDelegation
from factotum.contracts.actions import ActCode, ActOutcome, ActStatus
from factotum.contracts.errors import ActionsUnavailableError
from factotum.contracts.ids import DeviceId
from factotum.contracts.testing import make_tenant_id, make_tenant_principal, make_user_principal
from factotum.conversation.actions import ActionsClient

URL = "http://actions.test"
TENANT = make_tenant_id()
KITCHEN = DeviceId(uuid4())


@pytest.fixture
async def actions() -> AsyncIterator[ActionsClient]:
    settings = ServiceSettings(environment=Environment.TEST, _env_file=None)
    async with httpx.AsyncClient(base_url=URL) as client:
        yield ActionsClient(client, DevPrincipalDelegation(settings))


class TestAsking:
    @respx.mock
    async def test_what_came_of_the_message_comes_back(self, actions: ActionsClient) -> None:
        outcome = ActOutcome(status=ActStatus.DONE, reply="The lamp is on.", skill="lamp")
        respx.post(f"{URL}/v1/act").respond(200, json=outcome.model_dump(mode="json"))

        result = await actions.act(make_user_principal(tenant_id=TENANT), "turn on the lamp")

        assert result == outcome
        assert result.handled

    @respx.mock
    async def test_a_message_that_is_a_question_comes_back_as_one(
        self, actions: ActionsClient
    ) -> None:
        respx.post(f"{URL}/v1/act").respond(200, json=ActOutcome.no_skill().model_dump(mode="json"))

        result = await actions.act(make_user_principal(tenant_id=TENANT), "what is a router?")

        assert not result.handled

    @respx.mock
    async def test_a_message_that_could_not_be_read_comes_back_as_one(
        self, actions: ActionsClient
    ) -> None:
        respx.post(f"{URL}/v1/act").respond(200, json=ActOutcome.not_read().model_dump(mode="json"))

        result = await actions.act(make_user_principal(tenant_id=TENANT), "turn on the lamp")

        assert (result.handled, result.degraded) == (False, True)

    @respx.mock
    async def test_what_is_refused_comes_back_with_why(self, actions: ActionsClient) -> None:
        outcome = ActOutcome(status=ActStatus.REFUSED, reply="Not from here.", code=ActCode.DENIED)
        respx.post(f"{URL}/v1/act").respond(200, json=outcome.model_dump(mode="json"))

        result = await actions.act(make_tenant_principal(tenant_id=TENANT), "open the garage")

        assert (result.status, result.code) == (ActStatus.REFUSED, ActCode.DENIED)

    @respx.mock
    async def test_the_message_goes_alone_and_never_as_one_that_had_somebody_elses_words_in_it(
        self, actions: ActionsClient
    ) -> None:
        route = respx.post(f"{URL}/v1/act").respond(
            200, json=ActOutcome.no_skill().model_dump(mode="json")
        )

        await actions.act(make_user_principal(tenant_id=TENANT), "turn on the lamp")

        assert json.loads(route.calls.last.request.content) == {
            "message": "turn on the lamp",
            "untrusted_content_in_turn": False,
        }

    @respx.mock
    async def test_it_says_for_whom_and_from_which_device_it_asks(
        self, actions: ActionsClient
    ) -> None:
        route = respx.post(f"{URL}/v1/act").respond(
            200, json=ActOutcome.no_skill().model_dump(mode="json")
        )

        await actions.act(make_user_principal(tenant_id=TENANT, device_id=KITCHEN), "hello")
        personal = route.calls.last.request
        await actions.act(make_tenant_principal(tenant_id=TENANT), "hello")
        shared = route.calls.last.request

        assert (personal.headers[SUBJECT_HEADER], personal.headers[DEVICE_HEADER]) == (
            "user",
            str(KITCHEN),
        )
        assert shared.headers[SUBJECT_HEADER] == "tenant"
        assert DEVICE_HEADER not in shared.headers


class TestWhenItCannotBeAsked:
    @respx.mock
    async def test_a_service_that_cannot_be_reached_is_unavailable(
        self, actions: ActionsClient
    ) -> None:
        respx.post(f"{URL}/v1/act").mock(side_effect=httpx.ConnectError("refused"))

        with pytest.raises(ActionsUnavailableError, match="could not be reached"):
            await actions.act(make_user_principal(tenant_id=TENANT), "hello")

    @respx.mock
    async def test_a_service_that_takes_too_long_is_unavailable(
        self, actions: ActionsClient
    ) -> None:
        respx.post(f"{URL}/v1/act").mock(side_effect=httpx.ReadTimeout("too slow"))

        with pytest.raises(ActionsUnavailableError, match="could not be reached"):
            await actions.act(make_user_principal(tenant_id=TENANT), "hello")

    @pytest.mark.parametrize("status", [401, 403, 404, 422, 500, 503])
    @respx.mock
    async def test_an_error_is_unavailable_whatever_it_was(
        self, actions: ActionsClient, status: int
    ) -> None:
        respx.post(f"{URL}/v1/act").respond(status, json={"error": {"code": "x", "message": "y"}})

        with pytest.raises(ActionsUnavailableError, match=str(status)):
            await actions.act(make_user_principal(tenant_id=TENANT), "hello")

    @pytest.mark.parametrize(
        "body",
        [
            b"not json at all",
            b'{"status": "done"}',
            b'{"status": "levitating", "reply": "x"}',
            b'{"status": "no_skill", "reply": "a question has no reply"}',
            b"[]",
        ],
        ids=["text", "no-reply", "unknown-status", "incoherent", "list"],
    )
    @respx.mock
    async def test_something_that_is_not_an_outcome_is_unavailable(
        self, actions: ActionsClient, body: bytes
    ) -> None:
        respx.post(f"{URL}/v1/act").respond(200, content=body)

        with pytest.raises(ActionsUnavailableError, match="not an outcome"):
            await actions.act(make_user_principal(tenant_id=TENANT), "hello")

    @respx.mock
    async def test_the_message_is_never_in_what_it_says_about_it(
        self, actions: ActionsClient
    ) -> None:
        respx.post(f"{URL}/v1/act").respond(500, text="the message was: my secret plan")

        with pytest.raises(ActionsUnavailableError) as raised:
            await actions.act(make_user_principal(tenant_id=TENANT), "my secret plan")

        assert "secret" not in str(raised.value)
