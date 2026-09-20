"""What is particular to Postgres: the tables, the migration, and turns that begin together."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from factotum.contracts.identity import Owner, OwnerKind
from factotum.contracts.ids import DeviceId, TenantId
from factotum.conversation.store.postgres import PostgresStore, prepare, vector_sql
from factotum.db.pool import Database

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

NOW = datetime(2026, 9, 21, 9, 30, tzinfo=UTC)


class TestTheSchema:
    async def test_every_table_carries_its_tenant(self, conversation_database: Database) -> None:
        async with conversation_database.connection() as connection:
            without = await connection.fetch(
                "SELECT t.table_name FROM information_schema.tables t "
                "WHERE t.table_schema = 'conversation' AND t.table_name <> 'schema_migrations' "
                "AND NOT EXISTS (SELECT 1 FROM information_schema.columns c "
                "WHERE c.table_schema = t.table_schema AND c.table_name = t.table_name "
                "AND c.column_name = 'tenant_id')"
            )

        assert [row["table_name"] for row in without] == []

    async def test_the_migration_is_applied_once(self, conversation_database: Database) -> None:
        assert await prepare(conversation_database) == []

    async def test_what_belongs_to_nobody_in_particular_is_refused(
        self, conversation_database: Database
    ) -> None:
        async with conversation_database.connection() as connection:
            with pytest.raises(Exception, match="owner_kind"):
                await connection.execute(
                    "INSERT INTO conversation.conversations "
                    "(id, tenant_id, owner_kind, owner_id, started_at, last_at) "
                    "VALUES ($1, $2, 'device', $3, now(), now())",
                    uuid4(),
                    uuid4(),
                    uuid4(),
                )


class TestTheQueries:
    async def test_the_search_by_meaning_is_written_for_the_size_of_the_vectors(self) -> None:
        sql = vector_sql(1024)

        assert sql.count("vector(1024)") == 4
        assert "e.space_id = $7" in sql


class TestTurnsThatBeginTogether:
    async def test_two_turns_on_one_device_are_in_one_conversation(
        self, conversation_database: Database
    ) -> None:
        store = PostgresStore(conversation_database)
        tenant = TenantId(uuid4())
        owner = Owner(kind=OwnerKind.USER, id=uuid4())
        device = DeviceId(uuid4())

        opened = await asyncio.gather(
            *(
                store.open_conversation(
                    tenant,
                    owner,
                    device_id=device,
                    conversation_id=None,
                    now=NOW,
                    session_gap=timedelta(minutes=30),
                )
                for _ in range(8)
            )
        )

        assert len({conversation.id for conversation in opened}) == 1
