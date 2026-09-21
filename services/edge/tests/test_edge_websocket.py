"""The edge on a real socket: uvicorn serving it, and the client library that devices use talking to
it. What is tested is what only a real connection can show: that the door leads to a session, what
the peer sees when it is closed, and that a frame bigger than the protocol allows is refused by the
server before the edge reads it."""

import asyncio
from typing import Any

import httpx
import pytest
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from factotum.common.devauth import DEVICE_HEADER
from factotum.contracts.commands import CommandResult, SetVolume
from factotum.contracts.edge import (
    MAX_FRAME_BYTES,
    Ack,
    CloseCode,
    CommandRequest,
    Ping,
    Utterance,
    decode_edge_frame,
    encode_frame,
)
from factotum.edge.testing import Rig, forgotten, hello, serving


async def next_frame(ws: ClientConnection, timeout_s: float = 2.0) -> Any:
    return decode_edge_frame(await asyncio.wait_for(ws.recv(decode=True), timeout_s))


async def greeted(ws: ClientConnection, rig: Rig) -> Any:
    await ws.send(encode_frame(hello(rig.devices[0])))
    return await next_frame(ws)


class TestADeviceOnARealConnection:
    async def test_it_says_hello_is_told_it_is_ready_and_can_talk(self) -> None:
        async with Rig() as rig, serving(rig) as edge, connect(edge.websocket_url) as ws:
            ready = await greeted(ws, rig)
            await ws.send(encode_frame(Ping()))
            pong = await next_frame(ws)

            assert (ready.type, ready.name) == ("ready", "Kitchen speaker")
            assert pong.type == "pong"

    async def test_an_utterance_is_answered_in_parts_and_then_it_is_over(self) -> None:
        async with Rig() as rig, serving(rig) as edge, connect(edge.websocket_url) as ws:
            await greeted(ws, rig)

            await ws.send(encode_frame(Utterance(id="u1", text="hello")))
            kinds: list[str] = []
            while "turn_ended" not in kinds:
                kinds.append((await next_frame(ws)).type)

            # a hint of what is going on, the answer, and that it is over
            assert kinds == ["part", "part", "turn_ended"]

    async def test_a_device_that_is_not_known_is_told_so_and_the_door_is_closed(self) -> None:
        async with Rig() as rig, serving(rig) as edge, connect(edge.websocket_url) as ws:
            await ws.send(encode_frame(hello(rig.devices[0], token="not-the-credential")))

            error = await next_frame(ws)
            with pytest.raises(ConnectionClosed) as closed:
                await asyncio.wait_for(ws.recv(), 2)

            assert (error.type, error.code, error.fatal) == ("error", "auth_failed", True)
            assert closed.value.rcvd is not None
            assert closed.value.rcvd.code == CloseCode.POLICY

    async def test_what_is_not_text_is_not_a_frame(self) -> None:
        async with Rig() as rig, serving(rig) as edge, connect(edge.websocket_url) as ws:
            await ws.send(b"\x00\x01\x02")

            error = await next_frame(ws)
            with pytest.raises(ConnectionClosed) as closed:
                await asyncio.wait_for(ws.recv(), 2)

            assert error.code == "invalid_frame"
            assert closed.value.rcvd is not None
            assert closed.value.rcvd.code == CloseCode.PROTOCOL_ERROR

    async def test_a_frame_bigger_than_the_protocol_allows_is_refused_before_it_is_read(
        self,
    ) -> None:
        async with (
            Rig() as rig,
            serving(rig) as edge,
            connect(edge.websocket_url, max_size=None) as ws,
        ):
            await greeted(ws, rig)

            await ws.send(" " * (MAX_FRAME_BYTES + 1))
            with pytest.raises(ConnectionClosed) as closed:
                await asyncio.wait_for(ws.recv(), 2)

            assert closed.value.rcvd is not None
            assert closed.value.rcvd.code == CloseCode.TOO_BIG
            assert await forgotten(rig)

    async def test_a_device_that_hangs_up_is_forgotten(self) -> None:
        async with Rig() as rig, serving(rig) as edge:
            async with connect(edge.websocket_url) as ws:
                await greeted(ws, rig)
                assert len(rig.sessions) == 1

            assert await forgotten(rig)

    async def test_a_device_that_connects_again_replaces_the_connection_it_had(self) -> None:
        async with (
            Rig() as rig,
            serving(rig) as edge,
            connect(edge.websocket_url) as old,
            connect(edge.websocket_url) as new,
        ):
            await greeted(old, rig)
            await greeted(new, rig)

            with pytest.raises(ConnectionClosed) as closed:
                await asyncio.wait_for(old.recv(), 2)
            await new.send(encode_frame(Ping()))

            assert closed.value.rcvd is not None
            assert closed.value.rcvd.code == CloseCode.REPLACED
            assert (await next_frame(new)).type == "pong"

    async def test_when_the_edge_stops_the_devices_are_told_it_is_going_away(self) -> None:
        async with Rig() as rig, serving(rig) as edge, connect(edge.websocket_url) as ws:
            await greeted(ws, rig)
            await rig.sessions.close_all()

            with pytest.raises(ConnectionClosed) as closed:
                await asyncio.wait_for(ws.recv(), 2)

            assert closed.value.rcvd is not None
            assert closed.value.rcvd.code == CloseCode.GOING_AWAY


class TestACommandOverARealConnection:
    async def test_the_command_reaches_the_device_and_its_answer_comes_back_over_http(self) -> None:
        async with Rig() as rig, serving(rig) as edge, connect(edge.websocket_url) as ws:
            ready = await greeted(ws, rig)
            async with httpx.AsyncClient(base_url=edge.http_url) as http:
                asked = asyncio.create_task(
                    http.post(
                        f"/v1/devices/{ready.device_id}/commands",
                        content=CommandRequest(command=SetVolume(level=42)).model_dump_json(),
                        headers={
                            "content-type": "application/json",
                            DEVICE_HEADER: str(ready.device_id),
                        },
                    )
                )
                sent = await next_frame(ws)
                await ws.send(encode_frame(Ack(id=sent.id, result=CommandResult.ok())))
                response = await asked

            assert sent.type == "command"
            assert sent.command == SetVolume(level=42)
            assert (response.status_code, response.json()["status"]) == (200, "ok")
