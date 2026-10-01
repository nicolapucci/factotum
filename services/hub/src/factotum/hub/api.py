"""The HTTP interface of the hub.

``POST /v1/auth/login`` takes no authenticated caller: it is how one is made. Everything else is
under ``/v1`` and needs one — a token this very hub minted, since the hub is the root of trust and
there is nothing else to delegate the check to.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.types import Lifespan

from factotum.contracts.devices import ExternalDeviceRecord
from factotum.contracts.hub import (
    BindingList,
    BindingView,
    ChangePasswordRequest,
    CreateBindingRequest,
    CreateDeviceRequest,
    CreateUserRequest,
    DeviceList,
    LoginRequest,
    LoginResult,
    PairedDevice,
    UserList,
    UserView,
)
from factotum.hub.service import HubService
from factotum.web import Authenticator, Caller, create_base_app

MAX_BODY_BYTES = 16 * 1024
"""A login, a password change or a new user: never more than a few short fields."""


@dataclass(frozen=True, slots=True)
class Operations:
    """What the service does."""

    service: HubService


def _operations(request: Request) -> Operations:
    operations: Operations = request.app.state.operations
    return operations


Ops = Annotated[Operations, Depends(_operations)]


def create_app(
    authenticator: Authenticator,
    *,
    operations: Operations | None = None,
    docs: bool = False,
    lifespan: Lifespan[FastAPI] | None = None,
    ready: Callable[[], Awaitable[bool]] | None = None,
) -> FastAPI:
    """The hub as a web application. It is given the operations to serve (or, when it is put
    together while it starts, ``lifespan`` sets ``app.state.operations``) and, for ``/readyz``, a
    way to tell whether what it depends on answers."""
    app = create_base_app(
        "Factotum hub", authenticator, docs=docs, max_body_bytes=MAX_BODY_BYTES, lifespan=lifespan
    )
    app.state.operations = operations
    app.state.ready = ready

    @app.get("/healthz", summary="Liveness")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", summary="Readiness", response_model=None)
    async def readyz(request: Request) -> Response:
        check: Callable[[], Awaitable[bool]] | None = request.app.state.ready
        answering = True if check is None else await check()
        return JSONResponse(
            {"status": "ok" if answering else "unavailable"}, status_code=200 if answering else 503
        )

    @app.post("/v1/auth/login", summary="Log in with a username and a password")
    async def login(body: LoginRequest, ops: Ops) -> LoginResult:
        return await ops.service.login(body)

    @app.get("/v1/me", summary="The user of the caller's own token")
    async def me(principal: Caller, ops: Ops) -> UserView:
        return await ops.service.me(principal)

    @app.post(
        "/v1/auth/change-password",
        status_code=204,
        response_class=Response,
        summary="Change the caller's own password",
    )
    async def change_password(body: ChangePasswordRequest, principal: Caller, ops: Ops) -> Response:
        await ops.service.change_password(principal, body)
        return Response(status_code=204)

    @app.post(
        "/v1/users",
        status_code=201,
        summary="Add a user to the caller's tenant (an administrator only)",
    )
    async def create_user(body: CreateUserRequest, principal: Caller, ops: Ops) -> UserView:
        return await ops.service.create_user(principal, body)

    @app.get("/v1/users", summary="The users of the caller's tenant (an administrator only)")
    async def list_users(principal: Caller, ops: Ops) -> UserList:
        return await ops.service.list_users(principal)

    @app.post(
        "/v1/devices",
        status_code=201,
        summary="Register (pair) a device, system or external (an administrator only)",
    )
    async def create_device(
        body: CreateDeviceRequest, principal: Caller, ops: Ops
    ) -> PairedDevice | ExternalDeviceRecord:
        return await ops.service.create_device(principal, body)

    @app.get("/v1/devices", summary="Every device of the caller's tenant")
    async def list_devices(principal: Caller, ops: Ops) -> DeviceList:
        return await ops.service.list_devices(principal)

    @app.post(
        "/v1/bindings",
        status_code=201,
        summary="Bind a skill to a device, with its own parameters (an administrator only)",
    )
    async def create_binding(
        body: CreateBindingRequest, principal: Caller, ops: Ops
    ) -> BindingView:
        return await ops.service.create_binding(principal, body)

    @app.get("/v1/bindings", summary="Every binding of the caller's tenant")
    async def list_bindings(principal: Caller, ops: Ops) -> BindingList:
        return await ops.service.list_bindings(principal)

    return app
