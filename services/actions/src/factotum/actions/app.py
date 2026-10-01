"""Putting the service together from its settings: the database, the gateway, the devices, and
what runs a skill and gives a device its commands.

Everything that needs a running event loop (the connection pool, the clients of the other services,
the work in the background) is made when the application starts and closed when it stops, in its
lifespan.

What is not set is not there, and it is said when the service starts. Without a runner nothing is
run: a skill answers with what it would have run (``DryRunRunner``). Without an edge commands do not
reach a device (``NullDispatcher``). Without a file of secrets a skill that needs one does not run
(``NoSecrets``). There is nobody to ask for an approval until the hub (``NullApprover``).
"""

import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from fastapi import FastAPI

from factotum.actions.api import Operations, create_app
from factotum.actions.audit import AuditReader
from factotum.actions.catalog import Catalog
from factotum.actions.confirmations import ConfirmationReader
from factotum.actions.directory import (
    DeviceDirectory,
    FileDeviceDirectory,
    HubDeviceDirectory,
    load_directory,
)
from factotum.actions.edge_client import EdgeDispatcher
from factotum.actions.execution import (
    CommandDispatcher,
    DryRunRunner,
    NullDispatcher,
    SkillRunner,
)
from factotum.actions.instructions.writer import PlanWriter
from factotum.actions.matching import SkillIndex, SkillMatcher
from factotum.actions.planner import Planner
from factotum.actions.runner_client import RemoteRunner
from factotum.actions.secrets import NoSecrets, SecretResolver, load_secrets
from factotum.actions.service import ActionService
from factotum.actions.settings import ActionsSettings
from factotum.actions.store.postgres import PostgresStore, prepare
from factotum.common.devauth import DEV_TENANT_ID, DEV_USER_ID
from factotum.common.hubauth import authenticator_for, principal_delegation_for
from factotum.contracts.identity import SINGLE_TENANT_ID
from factotum.db.pool import Database
from factotum.devices.home import Home
from factotum.model_client import GatewayChat, GatewayEmbedder, delegation_for

logger = logging.getLogger("factotum.actions")

RUNNER_MARGIN_S = 5.0
"""What the runner is waited for beyond the time the service gives a run in all: the
service is the one that says a skill was too slow."""


def _file_directory(settings: ActionsSettings) -> DeviceDirectory:
    if settings.actions_devices_file is None:
        logger.warning("no file of devices is set: there are no devices to act on")
        return FileDeviceDirectory(settings, Home())
    return load_directory(
        settings.actions_devices_file, settings, tenant_id=DEV_TENANT_ID, user_id=DEV_USER_ID
    )


def _secrets(settings: ActionsSettings) -> SecretResolver:
    if settings.actions_secrets_file is None:
        logger.warning("no file of secrets is set: a skill that needs a secret will not run")
        return NoSecrets()
    return load_secrets(
        settings.actions_secrets_file, settings, tenant_id=DEV_TENANT_ID, user_id=DEV_USER_ID
    )


def build_app(settings: ActionsSettings) -> FastAPI:
    """The service as a web application.

    Authentication is the hub's once a token secret is configured (``FACTOTUM_HUB_TOKEN_SECRET``);
    otherwise the development authenticator, which refuses to exist outside development and test,
    so without one in production this raises and the service does not start. Proving an owner to
    the model gateway follows the same switch. The directory of devices follows it too: the hub's,
    fetched once in the lifespan, once ``FACTOTUM_ACTIONS_HUB_URL`` is also set; otherwise still a
    file, for development.
    """
    authenticator = authenticator_for(settings, tenant_id=DEV_TENANT_ID, user_id=DEV_USER_ID)
    delegation = delegation_for(settings, SINGLE_TENANT_ID)
    principals = principal_delegation_for(settings)
    hub_client: httpx.AsyncClient | None = None
    hub_directory: HubDeviceDirectory | None = None
    directory: DeviceDirectory
    if settings.hub_secret_bytes is not None and settings.actions_hub_url is not None:
        hub_client = httpx.AsyncClient(base_url=str(settings.actions_hub_url))
        hub_directory = HubDeviceDirectory(hub_client, principals, SINGLE_TENANT_ID)
        directory = hub_directory
    else:
        directory = _file_directory(settings)
    secrets = _secrets(settings)
    if settings.actions_runner_url is None:
        logger.warning("no runner is set: skills are not run, they answer with what they would run")
    if settings.actions_edge_url is None:
        logger.warning("no edge is set: the commands of skills do not reach a device")
    holder: dict[str, Database] = {}
    waits = (
        settings.actions_planner_timeout_s,
        settings.actions_reader_timeout_s,
        settings.actions_index_timeout_s,
        settings.actions_match_timeout_s,
    )
    model_timeout = httpx.Timeout(connect=3.0, read=max(waits) + 5.0, write=10.0, pool=5.0)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = await Database.connect(settings)
        holder["database"] = database
        try:
            await prepare(database)
            store = PostgresStore(database)
            async with AsyncExitStack() as clients:
                if hub_client is not None and hub_directory is not None:
                    clients.push_async_callback(hub_client.aclose)
                    await hub_directory.reload()
                gateway = await clients.enter_async_context(
                    httpx.AsyncClient(
                        base_url=str(settings.actions_gateway_url), timeout=model_timeout
                    )
                )
                runner: SkillRunner = DryRunRunner()
                if settings.actions_runner_url is not None:
                    runner = RemoteRunner(
                        await clients.enter_async_context(
                            httpx.AsyncClient(base_url=str(settings.actions_runner_url))
                        ),
                        store=store,
                        secrets=secrets,
                        delegation=principals,
                        margin_s=settings.actions_run_margin_s + RUNNER_MARGIN_S,
                    )
                dispatcher: CommandDispatcher = NullDispatcher()
                if settings.actions_edge_url is not None:
                    dispatcher = EdgeDispatcher(
                        await clients.enter_async_context(
                            httpx.AsyncClient(
                                base_url=str(settings.actions_edge_url),
                                timeout=httpx.Timeout(
                                    connect=3.0,
                                    read=settings.actions_edge_timeout_s,
                                    write=10.0,
                                    pool=5.0,
                                ),
                            )
                        ),
                        principals,
                    )
                chat = GatewayChat(gateway, delegation)
                writer = (
                    None
                    if settings.actions_runner_url is None
                    else PlanWriter(
                        chat,
                        timeout_s=settings.actions_plan_timeout_s,
                        attempts=settings.actions_plan_attempts,
                    )
                )
                embedder = GatewayEmbedder(gateway, delegation)
                index = SkillIndex(store, embedder, timeout_s=settings.actions_index_timeout_s)
                matcher = SkillMatcher(
                    store,
                    embedder,
                    index,
                    threshold=settings.actions_match_threshold,
                    max_candidates=settings.actions_match_candidates,
                    timeout_s=settings.actions_match_timeout_s,
                )
                app.state.operations = Operations(
                    service=ActionService(
                        store=store,
                        directory=directory,
                        matcher=matcher,
                        planner=Planner(chat, timeout_s=settings.actions_planner_timeout_s),
                        reader=ConfirmationReader(
                            chat, timeout_s=settings.actions_reader_timeout_s
                        ),
                        runner=runner,
                        dispatcher=dispatcher,
                        writer=writer,
                        settings=settings.action,
                    ),
                    catalog=Catalog(
                        store,
                        index,
                        folder=settings.actions_skills_dir,
                        enable_folder=settings.actions_enable_skills_dir,
                    ),
                    audit=AuditReader(store),
                )
                try:
                    yield
                finally:
                    await index.aclose()
        finally:
            await database.close()

    async def ready() -> bool:
        database = holder.get("database")
        return database is not None and await database.ping()

    return create_app(
        authenticator, docs=not settings.is_production, lifespan=lifespan, ready=ready
    )
