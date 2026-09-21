"""Putting the runner together from its settings: the engine, the sandbox, and the sweeper.

The runner is the only service that is given an engine to make containers with, and so the only one
that can be asked to make one: the caller is authenticated (the development authenticator, which
refuses to exist outside development and tests, until the hub issues real credentials), and what it
sends is a job, which cannot say what the container is like.

While it runs, a sweeper removes the containers of the runner that a runner that died left behind.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from factotum.common.devauth import DEV_TENANT_ID, DEV_USER_ID, DevAuthenticator
from factotum.runner.api import create_app
from factotum.runner.docker import DockerEngine, parse_host
from factotum.runner.sandbox import Sandbox
from factotum.runner.settings import RunnerSettings

logger = logging.getLogger("factotum.runner")

SWEEP_EVERY_S = 60.0


def engine_client(host: str) -> httpx.AsyncClient:
    """A client of the engine, on its socket or its port."""
    socket, base_url = parse_host(host)
    transport = None if socket is None else httpx.AsyncHTTPTransport(uds=socket)
    return httpx.AsyncClient(
        transport=transport,
        base_url=base_url,
        timeout=httpx.Timeout(30.0, connect=5.0),
    )


async def sweep(sandbox: Sandbox, *, every_s: float = SWEEP_EVERY_S) -> None:
    """Remove what a job left, now and every so often, for as long as it is running."""
    while True:
        removed = await sandbox.clean_up()
        if removed:
            logger.warning(
                "containers that were left behind were removed", extra={"count": removed}
            )
        await asyncio.sleep(every_s)


def build_app(settings: RunnerSettings) -> FastAPI:
    authenticator = DevAuthenticator(settings, tenant_id=DEV_TENANT_ID, user_id=DEV_USER_ID)
    holder: dict[str, Sandbox] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with engine_client(settings.runner_docker_host) as client:
            sandbox = Sandbox(DockerEngine(client), settings.sandbox)
            holder["sandbox"] = sandbox
            app.state.sandbox = sandbox
            sweeping = asyncio.create_task(sweep(sandbox), name="sweep-sandboxes")
            try:
                yield
            finally:
                sweeping.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sweeping

    async def ready() -> bool:
        sandbox = holder.get("sandbox")
        return sandbox is not None and await sandbox.ready()

    return create_app(
        authenticator, docs=not settings.is_production, lifespan=lifespan, ready=ready
    )
