"""The interfaces of the edge.

Devices connect to ``/v1/connect``, a WebSocket that speaks the protocol of
``factotum.contracts.edge``. The rest is HTTP, for the other services and for whoever looks after a
deployment: which devices are connected, and a way to have one of them do something, with what
became of it as the answer. A device that is not there is an answer (``offline``), not an error.
"""

import contextlib

from fastapi import FastAPI, Response, WebSocket
from fastapi.responses import JSONResponse
from starlette.types import Lifespan
from starlette.websockets import WebSocketDisconnect

from factotum.contracts.commands import CommandResult, CommandStatus
from factotum.contracts.edge import MAX_FRAME_BYTES, CommandRequest, ConnectedDevices
from factotum.contracts.errors import CapabilityNotSupportedError, DeviceNotFoundError
from factotum.contracts.ids import DeviceId
from factotum.edge.conversation import TurnSource
from factotum.edge.ports import Disconnected, SpeechSource
from factotum.edge.registry import DeviceRegistry
from factotum.edge.router import EdgeDevice, Sessions
from factotum.edge.session import Session, SessionSettings
from factotum.web import Authenticator, Caller, create_base_app

MAX_BODY_BYTES = MAX_FRAME_BYTES
"""What a request may hold: a command that carries a clip is a frame of the protocol, and not more
(it is a service that asks, and an authenticated one)."""


class StarletteWire:
    """A WebSocket of the web framework, as what a session needs of a connection."""

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket

    async def receive(self) -> str:
        try:
            message = await self._websocket.receive()
        except (WebSocketDisconnect, RuntimeError):
            raise Disconnected from None
        if message["type"] == "websocket.disconnect":
            raise Disconnected
        text = message.get("text")
        if text is None:
            # The protocol is text. Whatever else this is, it is not a frame: let it be refused.
            return bytes(message.get("bytes") or b"").decode("utf-8", errors="replace")
        return str(text)

    async def send(self, text: str) -> None:
        try:
            await self._websocket.send_text(text)
        except (WebSocketDisconnect, RuntimeError, OSError):
            raise Disconnected from None

    async def close(self, code: int, reason: str = "") -> None:
        # Already closed is what was wanted.
        with contextlib.suppress(WebSocketDisconnect, RuntimeError, OSError):
            await self._websocket.close(code=code, reason=reason)


def create_app(
    authenticator: Authenticator,
    *,
    sessions: Sessions,
    registry: DeviceRegistry,
    conversation: TurnSource,
    speech: SpeechSource | None = None,
    session_settings: SessionSettings | None = None,
    docs: bool = False,
    lifespan: Lifespan[FastAPI] | None = None,
) -> FastAPI:
    """The edge as a web application."""
    app = create_base_app(
        "Factotum edge",
        authenticator,
        docs=docs,
        max_body_bytes=MAX_BODY_BYTES,
        lifespan=lifespan,
    )

    @app.get("/healthz", summary="Liveness")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", summary="Readiness", response_model=None)
    async def readyz() -> Response:
        answering = await conversation.ready()
        return JSONResponse(
            {"status": "ok" if answering else "unavailable"}, status_code=200 if answering else 503
        )

    @app.get("/v1/devices", summary="The tenant's devices that are connected")
    async def connected(principal: Caller) -> ConnectedDevices:
        return ConnectedDevices(devices=tuple(sessions.connected(principal.tenant_id)))

    @app.post("/v1/devices/{device_id}/commands", summary="Have a device do something")
    async def send_command(
        device_id: DeviceId, body: CommandRequest, principal: Caller
    ) -> CommandResult:
        record = await registry.get(principal.tenant_id, device_id)
        if record is None:
            raise DeviceNotFoundError("the device does not exist")
        try:
            return await EdgeDevice(sessions, record).execute(body.command)
        except CapabilityNotSupportedError as error:
            return CommandResult(status=CommandStatus.UNSUPPORTED, detail=error.message)

    @app.websocket("/v1/connect")
    async def connect(websocket: WebSocket) -> None:
        await websocket.accept()
        await Session(
            StarletteWire(websocket),
            registry=registry,
            directory=sessions,
            conversation=conversation,
            speech=speech,
            settings=session_settings,
        ).run()

    return app


__all__ = ["MAX_BODY_BYTES", "MAX_FRAME_BYTES", "StarletteWire", "create_app"]
