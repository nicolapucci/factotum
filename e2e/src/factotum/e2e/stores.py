"""Where the services keep what they keep: in memory, or in the Postgres that ``docker compose up``
starts, as production does.

With Postgres every service has a pool and a schema of its own in a database that exists for the
test and is gone after it (``factotum.db.testing.temporary_database``), prepared by the same
migrations the service runs when it starts. Where there is no server the test is skipped, with a
message that says how to start one, and CI, which sets ``FACTOTUM_REQUIRE_INTEGRATION=1``, fails.
"""

from contextlib import AsyncExitStack
from dataclasses import dataclass
from enum import StrEnum

import asyncpg

from factotum.actions.store import ActionsStore
from factotum.actions.store import InMemoryStore as ActionsMemory
from factotum.actions.store.postgres import PostgresStore as ActionsPostgres
from factotum.actions.store.postgres import prepare as prepare_actions
from factotum.conversation.store import ConversationStore
from factotum.conversation.store import InMemoryStore as ConversationMemory
from factotum.conversation.store.postgres import PostgresStore as ConversationPostgres
from factotum.conversation.store.postgres import prepare as prepare_conversation
from factotum.db.pool import Database
from factotum.db.settings import DatabaseNotConfiguredError
from factotum.db.testing import skip_or_fail, temporary_database
from factotum.hub.store.base import HubStore
from factotum.hub.store.memory import InMemoryStore as HubMemory
from factotum.hub.store.postgres import PostgresStore as HubPostgres
from factotum.hub.store.postgres import prepare as prepare_hub
from factotum.knowledge.store import InMemoryStore as KnowledgeMemory
from factotum.knowledge.store import KnowledgeStore
from factotum.knowledge.store.postgres import PostgresStore as KnowledgePostgres
from factotum.knowledge.store.postgres import prepare as prepare_knowledge


class Store(StrEnum):
    MEMORY = "memory"
    POSTGRES = "postgres"


@dataclass
class Stores:
    knowledge: KnowledgeStore
    conversation: ConversationStore
    actions: ActionsStore
    hub: HubStore


def in_memory() -> Stores:
    return Stores(KnowledgeMemory(), ConversationMemory(), ActionsMemory(), HubMemory())


async def in_postgres(closing: AsyncExitStack) -> Stores:
    """A database for this test, and a pool, prepared, for each service. Everything is closed, and
    the database dropped, when ``closing`` is."""
    try:
        settings = await closing.enter_async_context(temporary_database())
        databases = [await Database.connect(settings) for _ in range(4)]
    except (OSError, asyncpg.PostgresError, TimeoutError, DatabaseNotConfiguredError) as error:
        skip_or_fail(error)
    for database in databases:
        closing.push_async_callback(database.close)
    knowledge, conversation, actions, hub = databases
    await prepare_knowledge(knowledge)
    await prepare_conversation(conversation)
    await prepare_actions(actions)
    await prepare_hub(hub)
    return Stores(
        KnowledgePostgres(knowledge),
        ConversationPostgres(conversation),
        ActionsPostgres(actions),
        HubPostgres(hub),
    )
