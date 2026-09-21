"""The WebSocket connection of the client, against a server of the library that does what a test
asks: echoes, closes with a code, or is cut."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import pytest
from websockets.asyncio.server import ServerConnection, serve

from factotum.terminal.connection import (
    CLOSED_ABNORMALLY,
    ConnectionLost,
    CouldNotConnect,
    WebSocketConnection,
)

Handler = Callable[[ServerConnection], Awaitable[None]]


@asynccontextmanager
async def serving(handler: Handler) -> AsyncIterator[str]:
    """A server on a loopback port that the system chooses, and its address."""
    async with serve(handler, "127.0.0.1", 0) as running:
        port = next(iter(running.sockets)).getsockname()[1]
        yield f"ws://127.0.0.1:{port}/v1/connect"


async def echo(connection: ServerConnection) -> None:
    async for message in connection:
        await connection.send(message)


class TestWhatIsSaid:
    async def test_what_is_sent_comes_back_as_it_was_sent(self) -> None:
        async with serving(echo) as url:
            connection = await WebSocketConnection.open(url)

            await connection.send('{"type": "ping"}')
            said = await connection.recv()
            await connection.close()

        assert said == '{"type": "ping"}'

    async def test_a_binary_frame_is_read_as_the_text_it_carries(self) -> None:
        async def binary(connection: ServerConnection) -> None:
            await connection.send("café".encode())
            await connection.wait_closed()

        async with serving(binary) as url:
            connection = await WebSocketConnection.open(url)

            said = await connection.recv()
            await connection.close()

        assert said == "café"

    async def test_it_can_be_closed_more_than_once(self) -> None:
        async with serving(echo) as url:
            connection = await WebSocketConnection.open(url)

            await connection.close()
            await connection.close()


class TestWhenItEnds:
    async def test_the_edge_closing_it_says_the_code_and_the_reason(self) -> None:
        async def replaced(connection: ServerConnection) -> None:
            await connection.close(4001, "replaced")

        async with serving(replaced) as url:
            connection = await WebSocketConnection.open(url)

            with pytest.raises(ConnectionLost) as lost:
                await connection.recv()

        assert (lost.value.code, lost.value.reason) == (4001, "replaced")

    async def test_a_connection_that_is_cut_with_no_word_is_an_abnormal_closure(self) -> None:
        async def cut(connection: ServerConnection) -> None:
            connection.transport.abort()

        async with serving(cut) as url:
            connection = await WebSocketConnection.open(url)

            with pytest.raises(ConnectionLost) as lost:
                await connection.recv()

        assert lost.value.code == CLOSED_ABNORMALLY

    async def test_nothing_can_be_sent_once_the_edge_has_gone(self) -> None:
        async def gone(connection: ServerConnection) -> None:
            await connection.close(1001)

        async with serving(gone) as url:
            connection = await WebSocketConnection.open(url)
            with pytest.raises(ConnectionLost):
                await connection.recv()

            with pytest.raises(ConnectionLost) as lost:
                await connection.send("too late")

        assert lost.value.code == 1001


class TestWhenThereIsNoConnection:
    async def test_an_address_where_nobody_listens_is_an_edge_that_could_not_be_reached(
        self,
    ) -> None:
        async with serving(echo) as url:
            pass  # the server is gone once the block is left: the port is free again

        with pytest.raises(CouldNotConnect, match=r"could not connect to ws://127\.0\.0\.1"):
            await WebSocketConnection.open(url)

    async def test_a_server_that_does_not_speak_websocket_is_not_an_edge(self) -> None:
        async def plain_http(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 0\r\nconnection: close\r\n\r\n")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(plain_http, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            with pytest.raises(CouldNotConnect):
                await WebSocketConnection.open(f"ws://127.0.0.1:{port}/v1/connect")
        finally:
            server.close()
            await server.wait_closed()
