"""What the client needs of a connection to the edge: frames in and out, and a way to end it.

The real one is a WebSocket; the tests use one in memory (``factotum.terminal.testing``).
"""

from typing import Protocol

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI

from factotum.contracts.edge import MAX_FRAME_BYTES


class ConnectionLost(Exception):  # noqa: N818 - it says what happened, not what failed
    """The connection is over: closed by the edge, or dropped. ``code`` and ``reason`` say how."""

    def __init__(self, code: int | None = None, reason: str = "") -> None:
        super().__init__(reason or f"the connection closed ({code})")
        self.code = code
        self.reason = reason


class CouldNotConnect(Exception):  # noqa: N818
    """The edge could not be reached, or did not accept the connection."""


class Connection(Protocol):
    async def send(self, text: str) -> None:
        """Send a frame. Raises ``ConnectionLost`` when it cannot be sent."""
        ...

    async def recv(self) -> str:
        """The next frame. Raises ``ConnectionLost`` when the connection is over."""
        ...

    async def close(self) -> None: ...


class WebSocketConnection:
    """A WebSocket to the edge."""

    def __init__(self, websocket: ClientConnection) -> None:
        self._websocket = websocket

    @classmethod
    async def open(cls, url: str) -> "WebSocketConnection":
        try:
            # A frame is at most what the protocol allows; the library keeps the connection alive at
            # its own level (pings), beside the heartbeat of the protocol.
            websocket = await connect(url, max_size=MAX_FRAME_BYTES, open_timeout=10)
        except (OSError, TimeoutError, InvalidHandshake, InvalidURI) as error:
            raise CouldNotConnect(f"could not connect to {url}: {error}") from None
        return cls(websocket)

    async def send(self, text: str) -> None:
        try:
            await self._websocket.send(text)
        except ConnectionClosed as closed:
            raise _lost(closed) from None

    async def recv(self) -> str:
        try:
            message = await self._websocket.recv()
        except ConnectionClosed as closed:
            raise _lost(closed) from None
        return message if isinstance(message, str) else message.decode("utf-8", errors="replace")

    async def close(self) -> None:
        await self._websocket.close()


CLOSED_ABNORMALLY = 1006
"""What the standard says a connection that ended with no close frame (a cable, a crash) was."""


def _lost(closed: ConnectionClosed) -> ConnectionLost:
    received = closed.rcvd
    if received is None:
        return ConnectionLost(CLOSED_ABNORMALLY, "")
    return ConnectionLost(received.code, received.reason)
