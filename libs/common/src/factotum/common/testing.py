"""Support for tests that need a real connection: a server on the loopback that keeps them open."""

import asyncio
import contextlib
from types import TracebackType
from typing import Self

_LOOPBACK = "127.0.0.1"


def _headers(head: bytes) -> dict[str, str]:
    lines = head.decode("latin-1").split("\r\n")[1:]
    return {
        name.lower(): value for name, _, value in (line.partition(": ") for line in lines if line)
    }


class KeepAliveServer:
    """An HTTP/1.1 server on the loopback that answers ``ok`` to everything, keeps its connections
    open, and counts them: what a test needs to see whether a client connects again or reuses the
    connection it has. With ``cookie`` it also sets one in every answer.

    Used as ``async with KeepAliveServer() as server``.
    """

    def __init__(self, *, cookie: bool = False) -> None:
        self.connections = 0
        """How many connections were made to it."""
        self.open = 0
        """How many are open now."""
        self.requests: list[dict[str, str]] = []
        """The headers of every request served, with their names in lower case."""
        self.cookie = cookie
        self.port = 0
        self._all_closed = asyncio.Event()
        self._all_closed.set()
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def __aenter__(self) -> Self:
        self._server = await asyncio.start_server(self._serve, _LOOPBACK, 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._server is not None:
            self._server.close()
            for writer in list(self._writers):
                writer.close()
            await self._server.wait_closed()
            self._server = None

    def url(self, host: str = _LOOPBACK, path: str = "/") -> str:
        """Where to reach it, under any host name that resolves to the loopback."""
        return f"http://{host}:{self.port}{path}"

    async def until_closed(self, seconds: float = 5.0) -> None:
        """Wait until the clients have closed every connection; a test that expects them closed
        fails here, with a timeout, if one was left open."""
        async with asyncio.timeout(seconds):
            await self._all_closed.wait()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self.open += 1
        self._all_closed.clear()
        self._writers.add(writer)
        try:
            with contextlib.suppress(asyncio.IncompleteReadError, ConnectionError):
                while True:  # until the client closes the connection
                    headers = _headers(await reader.readuntil(b"\r\n\r\n"))
                    await reader.readexactly(int(headers.get("content-length", "0")))
                    self.requests.append(headers)
                    cookie = "Set-Cookie: session=1\r\n" if self.cookie else ""
                    writer.write(f"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n{cookie}\r\nok".encode())
                    await writer.drain()
        finally:
            self.open -= 1
            if not self.open:
                self._all_closed.set()
            self._writers.discard(writer)
            writer.close()
