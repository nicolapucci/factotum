"""Giving a device its commands through the edge: what is sent, and that whatever becomes of a
command comes back as a result and is never raised."""

import json
from typing import Any

import httpx
import pytest

from factotum.actions.edge_client import EdgeDispatcher
from factotum.common.config import Environment, ServiceSettings
from factotum.common.devauth import DevPrincipalDelegation
from factotum.contracts.commands import CommandResult, CommandStatus, SetVolume, Speak
from factotum.contracts.ids import DeviceId
from factotum.contracts.testing import make_device_id, make_tenant_id

TEST = ServiceSettings(_env_file=None, environment=Environment.TEST)
TENANT = make_tenant_id()
DEVICE: DeviceId = make_device_id()


class Edge:
    """An edge that records what it is sent and answers with what it is told to."""

    def __init__(self) -> None:
        self.sent: list[httpx.Request] = []
        self.reply: Any = lambda request: httpx.Response(200, json={"status": "ok"})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        response: httpx.Response = self.reply(request)
        return response

    def dispatcher(self) -> EdgeDispatcher:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self), base_url="http://edge.test")
        return EdgeDispatcher(client, DevPrincipalDelegation(TEST))


class TestWhatIsSent:
    async def test_the_command_goes_to_the_device_it_is_for(self) -> None:
        edge = Edge()

        await edge.dispatcher().dispatch(TENANT, DEVICE, SetVolume(level=40))

        sent = edge.sent[0]
        assert (sent.method, sent.url.path) == ("POST", f"/v1/devices/{DEVICE}/commands")
        assert json.loads(sent.content) == {"command": {"name": "set_volume", "level": 40}}

    async def test_it_acts_for_the_tenant_and_not_for_a_person(self) -> None:
        edge = Edge()

        await edge.dispatcher().dispatch(TENANT, DEVICE, Speak(text="Hello"))

        assert edge.sent[0].headers["x-factotum-dev-subject"] == "tenant"

    async def test_what_the_edge_says_became_of_it_is_what_comes_back(self) -> None:
        edge = Edge()

        result = await edge.dispatcher().dispatch(TENANT, DEVICE, SetVolume(level=40))

        assert result == CommandResult.ok()


class TestWhatBecameOfIt:
    @pytest.mark.parametrize("status", list(CommandStatus), ids=lambda status: status.value)
    async def test_a_device_that_could_not_do_it_is_a_result_and_not_an_error(
        self, status: CommandStatus
    ) -> None:
        edge = Edge()
        edge.reply = lambda request: httpx.Response(
            200, json={"status": status.value, "detail": "as said by the device"}
        )

        result = await edge.dispatcher().dispatch(TENANT, DEVICE, SetVolume(level=40))

        assert (result.status, result.detail) == (status, "as said by the device")

    @pytest.mark.parametrize(
        "raised",
        [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("x")],
        ids=lambda error: type(error).__name__,
    )
    async def test_an_edge_that_cannot_be_reached_is_a_command_that_failed(
        self, raised: httpx.HTTPError
    ) -> None:
        edge = Edge()

        def refuse(request: httpx.Request) -> httpx.Response:
            raise raised

        edge.reply = refuse

        result = await edge.dispatcher().dispatch(TENANT, DEVICE, SetVolume(level=40))

        assert result.status is CommandStatus.FAILED
        assert result.detail == "the edge could not be reached"

    @pytest.mark.parametrize("status", [401, 404, 422, 500, 503])
    async def test_an_edge_that_answers_with_an_error_is_a_command_that_failed(
        self, status: int
    ) -> None:
        edge = Edge()
        edge.reply = lambda request: httpx.Response(status, json={"error": {"code": "x"}})

        result = await edge.dispatcher().dispatch(TENANT, DEVICE, SetVolume(level=40))

        assert result.status is CommandStatus.FAILED
        assert result.detail == f"the edge answered with an error ({status})"

    @pytest.mark.parametrize("body", ["nope", "[]", '{"status": "maybe"}', '{"detail": "x"}'])
    async def test_an_answer_that_is_not_the_result_of_a_command_is_a_command_that_failed(
        self, body: str
    ) -> None:
        edge = Edge()
        edge.reply = lambda request: httpx.Response(200, text=body)

        result = await edge.dispatcher().dispatch(TENANT, DEVICE, SetVolume(level=40))

        assert result.status is CommandStatus.FAILED
        assert result.detail is not None
        assert "not the result of a command" in result.detail

    async def test_a_command_that_the_device_does_not_understand_comes_back_as_it_was_said(
        self,
    ) -> None:
        edge = Edge()
        edge.reply = lambda request: httpx.Response(
            200, json={"status": "unsupported", "detail": "no speaker"}
        )

        result = await edge.dispatcher().dispatch(TENANT, DEVICE, Speak(text="Hello"))

        assert result.status is CommandStatus.UNSUPPORTED
        assert not result.succeeded
