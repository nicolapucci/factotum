"""Putting the service together from its settings: the database, the gateway, the devices.

Everything that needs a running event loop (the connection pool, the client of the gateway, the work
in the background) is made when the application starts and closed when it stops, in its lifespan.

Until slice 5b nothing here runs a skill for real: the runner is the one that says what it would
have run (``DryRunRunner``), commands do not reach a device (``NullDispatcher``) and there is nobody
to ask for an approval (``NullApprover``). Everything else is real.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from factotum.actions.api import Operations, create_app
from factotum.actions.audit import AuditReader
from factotum.actions.catalog import Catalog
from factotum.actions.confirmations import ConfirmationReader
from factotum.actions.directory import DeviceDirectory, FileDeviceDirectory, load_directory
from factotum.actions.execution import DryRunRunner
from factotum.actions.matching import SkillIndex, SkillMatcher
from factotum.actions.planner import Planner
from factotum.actions.service import ActionService
from factotum.actions.settings import ActionsSettings
from factotum.actions.store.postgres import PostgresStore, prepare
from factotum.common.devauth import DEV_TENANT_ID, DEV_USER_ID, DevAuthenticator
from factotum.db.pool import Database
from factotum.devices.home import Home
from factotum.model_client import DevDelegation, GatewayChat, GatewayEmbedder

logger = logging.getLogger("factotum.actions")


def _directory(settings: ActionsSettings) -> DeviceDirectory:
    if settings.actions_devices_file is None:
        logger.warning("no file of devices is set: there are no devices to act on")
        return FileDeviceDirectory(settings, Home())
    return load_directory(
        settings.actions_devices_file, settings, tenant_id=DEV_TENANT_ID, user_id=DEV_USER_ID
    )


def build_app(settings: ActionsSettings) -> FastAPI:
    """The service as a web application.

    Until the hub issues real credentials the only way to authenticate is the development
    authenticator, and the only knowledge of the devices is a file: both refuse to exist outside
    development and test, so in production this raises, and the service does not start.
    """
    authenticator = DevAuthenticator(settings, tenant_id=DEV_TENANT_ID, user_id=DEV_USER_ID)
    delegation = DevDelegation(settings)
    directory = _directory(settings)
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
            async with httpx.AsyncClient(
                base_url=str(settings.actions_gateway_url), timeout=model_timeout
            ) as gateway:
                chat = GatewayChat(gateway, delegation)
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
                        runner=DryRunRunner(),
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
