"""The HTTP interface of the actions service.

Everything is under ``/v1`` and needs an authenticated caller. ``POST /v1/act`` is what the
conversation service asks, before it searches the documents, about every message: it answers that
the message is not an action, or what became of it, with the words to say. The skills and the audit
trail are administration, and the routes for it never serve a call that is made through a device.
What makes an answer impossible (a skill that is not there, a caller who may not) is an HTTP error
with its own status, like the other services.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse
from starlette.types import Lifespan

from factotum.actions.audit import MAX_PAGE, AuditReader
from factotum.actions.catalog import Catalog
from factotum.actions.service import ActionService
from factotum.contracts.actions import (
    ActOutcome,
    ActRequest,
    AuditList,
    SkillDetail,
    SkillEnable,
    SkillInstall,
    SkillList,
    SkillReload,
)
from factotum.contracts.ids import DeviceId, SkillId
from factotum.web import Authenticator, Caller, create_base_app

MAX_BODY_BYTES = 512 * 1024
"""A ``SKILL.md`` is at most 100 000 characters, which is a good deal more than that in bytes only
when it is not written in a language that fits in one byte for each."""


@dataclass(frozen=True, slots=True)
class Operations:
    """What the service does."""

    service: ActionService
    catalog: Catalog
    audit: AuditReader


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
    """The service as a web application. It is given the operations to serve (or, when it is put
    together while it starts, ``lifespan`` sets ``app.state.operations``) and, for ``/readyz``, a
    way to tell whether what it depends on answers."""
    app = create_base_app(
        "Factotum actions",
        authenticator,
        docs=docs,
        max_body_bytes=MAX_BODY_BYTES,
        lifespan=lifespan,
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

    @app.post("/v1/act", summary="Is a message a request to do something, and what came of it")
    async def act(body: ActRequest, principal: Caller, ops: Ops) -> ActOutcome:
        return await ops.service.act(principal, body)

    @app.get("/v1/skills", summary="The skills the caller may see")
    async def list_skills(principal: Caller, ops: Ops) -> SkillList:
        return await ops.catalog.list(principal)

    @app.post(
        "/v1/skills",
        summary="Install a skill, from the text of its SKILL.md (it does not run until enabled)",
        responses={200: {"description": "The very same skill was installed already"}},
        status_code=201,
    )
    async def install_skill(
        body: SkillInstall, principal: Caller, ops: Ops, response: Response
    ) -> SkillDetail:
        detail, created = await ops.catalog.install(principal, body)
        if not created:
            response.status_code = 200
        return detail

    @app.post("/v1/skills/reload", summary="Read the folder of skills again")
    async def reload_skills(principal: Caller, ops: Ops) -> SkillReload:
        return await ops.catalog.reload(principal)

    @app.get("/v1/skills/{skill_id}", summary="A skill, its versions and what is granted")
    async def get_skill(skill_id: SkillId, principal: Caller, ops: Ops) -> SkillDetail:
        return await ops.catalog.get(principal, skill_id)

    @app.post(
        "/v1/skills/{skill_id}/enable",
        summary="Review a version (the latest, unless one is named) and enable it",
    )
    async def enable_skill(
        skill_id: SkillId, principal: Caller, ops: Ops, body: SkillEnable | None = None
    ) -> SkillDetail:
        return await ops.catalog.enable(principal, skill_id, body or SkillEnable())

    @app.post("/v1/skills/{skill_id}/disable", summary="Nothing runs of the skill any more")
    async def disable_skill(skill_id: SkillId, principal: Caller, ops: Ops) -> SkillDetail:
        return await ops.catalog.disable(principal, skill_id)

    @app.delete(
        "/v1/skills/{skill_id}",
        status_code=204,
        response_class=Response,
        summary="Delete a skill, with its versions",
    )
    async def delete_skill(skill_id: SkillId, principal: Caller, ops: Ops) -> Response:
        await ops.catalog.delete(principal, skill_id)
        return Response(status_code=204)

    @app.get("/v1/audit", summary="What was done, and refused: the latest first")
    async def audit(
        principal: Caller,
        ops: Ops,
        before: Annotated[datetime | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50,
        skill: Annotated[str | None, Query(max_length=64)] = None,
        device: Annotated[DeviceId | None, Query()] = None,
    ) -> AuditList:
        return await ops.audit.list(
            principal, before=before, limit=limit, skill_name=skill, device_id=device
        )

    return app
