"""The HTTP interface of the runner.

One thing is asked of it: ``POST /v1/jobs`` runs a job and answers what it did. A script that failed
is an answer (its exit code, what it wrote, that it was too slow); what is an error is that nothing
could run it (the engine is not there, the runner is busy), with the status of a service that is
not available.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.types import Lifespan

from factotum.contracts.errors import RunnerUnavailableError
from factotum.contracts.runner import Job, JobResult
from factotum.runner.sandbox import Sandbox, SandboxUnavailable
from factotum.web import Authenticator, Caller, create_base_app

MAX_BODY_BYTES = 8 * 1024 * 1024
"""The most that a job may be: the files of a folder, in base64, and what they are given."""


def _sandbox(request: Request) -> Sandbox:
    sandbox: Sandbox = request.app.state.sandbox
    return sandbox


Runs = Annotated[Sandbox, Depends(_sandbox)]


def create_app(
    authenticator: Authenticator,
    *,
    sandbox: Sandbox | None = None,
    docs: bool = False,
    lifespan: Lifespan[FastAPI] | None = None,
    ready: Callable[[], Awaitable[bool]] | None = None,
) -> FastAPI:
    """The runner as a web application. It is given the sandbox to run jobs in (or, when it is put
    together while it starts, ``lifespan`` sets ``app.state.sandbox``) and, for ``/readyz``, a way
    to tell whether jobs can be run."""
    app = create_base_app(
        "Factotum runner",
        authenticator,
        docs=docs,
        max_body_bytes=MAX_BODY_BYTES,
        lifespan=lifespan,
    )
    app.state.sandbox = sandbox
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

    @app.post("/v1/jobs", summary="Run a script in a container of its own")
    async def run_job(job: Job, principal: Caller, sandbox: Runs) -> JobResult:
        try:
            return await sandbox.run(job)
        except SandboxUnavailable as error:
            raise RunnerUnavailableError(str(error)) from None

    return app
