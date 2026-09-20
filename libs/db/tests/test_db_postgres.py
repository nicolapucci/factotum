"""Against a real Postgres: it needs the database of ``docker compose up``."""

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from factotum.contracts.ids import TenantId
from factotum.contracts.testing import make_tenant_id
from factotum.db.migrations import Migration, MigrationError, migrate
from factotum.db.pool import Database
from factotum.db.settings import DatabaseNotConfiguredError, DatabaseSettings
from factotum.db.testing import server_settings, temporary_database

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def database(database_settings: DatabaseSettings) -> AsyncIterator[Database]:
    db = await Database.connect(database_settings)
    yield db
    await db.close()


def schema_name() -> str:
    return f"t_{uuid4().hex[:10]}"


THINGS = [
    Migration(1, "create_things", "CREATE TABLE {s}.things (id integer PRIMARY KEY, name text)"),
    Migration(2, "add_size", "ALTER TABLE {s}.things ADD COLUMN size integer"),
    Migration(3, "index_names", "CREATE INDEX things_name ON {s}.things (name)"),
]


def things(schema: str) -> list[Migration]:
    return [Migration(m.version, m.name, m.sql.format(s=schema)) for m in THINGS]


class TestDatabase:
    async def test_it_answers_a_ping(self, database: Database) -> None:
        assert await database.ping()

    async def test_a_transaction_commits_when_the_block_ends(self, database: Database) -> None:
        schema = schema_name()
        async with database.connection() as connection:
            await migrate(connection, schema, things(schema))

        async with database.transaction() as connection:
            await connection.execute(f"INSERT INTO {schema}.things (id, name) VALUES (1, 'a')")  # noqa: S608

        async with database.connection() as connection:
            count = await connection.fetchval(f"SELECT count(*) FROM {schema}.things")  # noqa: S608
        assert count == 1

    async def test_a_transaction_rolls_back_when_the_block_raises(self, database: Database) -> None:
        schema = schema_name()
        async with database.connection() as connection:
            await migrate(connection, schema, things(schema))

        async def failing() -> None:
            async with database.transaction() as connection:
                await connection.execute(f"INSERT INTO {schema}.things (id, name) VALUES (1, 'a')")  # noqa: S608
                raise RuntimeError("stop")

        with pytest.raises(RuntimeError, match="stop"):
            await failing()

        async with database.connection() as connection:
            count = await connection.fetchval(f"SELECT count(*) FROM {schema}.things")  # noqa: S608
        assert count == 0

    async def test_the_transaction_tells_the_database_which_tenant_it_works_for(
        self, database: Database
    ) -> None:
        tenant = make_tenant_id()

        async with database.transaction(tenant) as connection:
            inside = await connection.fetchval("SELECT current_setting('app.tenant_id')")

        assert inside == str(tenant)

    async def test_the_tenant_does_not_outlive_its_transaction(self, database: Database) -> None:
        async with database.transaction(make_tenant_id()):
            pass

        async with database.transaction() as connection:
            after = await connection.fetchval("SELECT current_setting('app.tenant_id', true)")

        assert not after

    async def test_transactions_of_different_tenants_do_not_see_each_others_setting(
        self, database: Database
    ) -> None:
        first, second = make_tenant_id(), make_tenant_id()

        async def seen(tenant: TenantId) -> str:
            async with database.transaction(tenant) as connection:
                await asyncio.sleep(0.05)
                return str(await connection.fetchval("SELECT current_setting('app.tenant_id')"))

        first_saw, second_saw = await asyncio.gather(seen(first), seen(second))

        assert (first_saw, second_saw) == (str(first), str(second))

    async def test_a_server_that_is_not_there_is_an_os_error(self) -> None:
        settings = server_settings().model_copy(update={"postgres_port": 1})

        with pytest.raises(ConnectionRefusedError):
            await Database.connect(settings, min_size=1)

    async def test_a_missing_password_is_a_configuration_error(self) -> None:
        settings = DatabaseSettings(_env_file=None, postgres_password=None)

        with pytest.raises(DatabaseNotConfiguredError):
            await Database.connect(settings)

    async def test_a_closed_database_does_not_answer_a_ping(
        self, database_settings: DatabaseSettings
    ) -> None:
        db = await Database.connect(database_settings)
        await db.close()

        assert await db.ping() is False


class TestMigrate:
    async def test_it_applies_every_migration_in_order_and_says_which(
        self, database: Database
    ) -> None:
        schema = schema_name()

        async with database.connection() as connection:
            done = await migrate(connection, schema, things(schema))
            columns = await connection.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = $1 AND table_name = 'things'",
                schema,
            )

        assert done == [1, 2, 3]
        assert {row["column_name"] for row in columns} == {"id", "name", "size"}

    async def test_a_second_run_does_nothing(self, database: Database) -> None:
        schema = schema_name()
        async with database.connection() as connection:
            await migrate(connection, schema, things(schema))

            assert await migrate(connection, schema, things(schema)) == []

    async def test_a_new_migration_is_applied_on_top_of_the_ones_already_there(
        self, database: Database
    ) -> None:
        schema = schema_name()
        async with database.connection() as connection:
            await migrate(connection, schema, things(schema)[:2])

            assert await migrate(connection, schema, things(schema)) == [3]

    async def test_what_was_applied_is_recorded_with_its_checksum(self, database: Database) -> None:
        schema = schema_name()
        async with database.connection() as connection:
            await migrate(connection, schema, things(schema))
            rows = await connection.fetch(
                f"SELECT version, name, checksum FROM {schema}.schema_migrations ORDER BY version"  # noqa: S608
            )

        assert [(r["version"], r["name"]) for r in rows] == [
            (1, "create_things"),
            (2, "add_size"),
            (3, "index_names"),
        ]
        assert rows[0]["checksum"] == things(schema)[0].checksum

    async def test_a_migration_edited_after_it_was_applied_is_refused(
        self, database: Database
    ) -> None:
        schema = schema_name()
        async with database.connection() as connection:
            await migrate(connection, schema, things(schema))
            edited = things(schema)
            edited[1] = Migration(2, "add_size", edited[1].sql + " -- a change")

            with pytest.raises(MigrationError, match="was edited after it was applied"):
                await migrate(connection, schema, edited)

    async def test_a_database_newer_than_the_code_is_refused(self, database: Database) -> None:
        schema = schema_name()
        async with database.connection() as connection:
            await migrate(connection, schema, things(schema))

            with pytest.raises(MigrationError, match="newer than the service"):
                await migrate(connection, schema, things(schema)[:2])

    async def test_a_migration_that_fails_leaves_nothing_behind(self, database: Database) -> None:
        schema = schema_name()
        broken = [
            *things(schema)[:1],
            Migration(2, "broken", f"ALTER TABLE {schema}.things ADD COLUMN x integer; SELECT 1/0"),
        ]
        async with database.connection() as connection:
            with pytest.raises(asyncpg.DivisionByZeroError):
                await migrate(connection, schema, broken)

            columns = await connection.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = $1 AND table_name = 'things'",
                schema,
            )
            recorded = await connection.fetch(f"SELECT version FROM {schema}.schema_migrations")  # noqa: S608

        assert {row["column_name"] for row in columns} == {"id", "name"}
        assert [row["version"] for row in recorded] == [1]

    async def test_two_runners_starting_together_apply_each_migration_once(
        self, database: Database
    ) -> None:
        schema = schema_name()

        async def run() -> list[int]:
            async with database.connection() as connection:
                return await migrate(connection, schema, things(schema))

        first, second = await asyncio.gather(run(), run())

        assert sorted(first + second) == [1, 2, 3]

    async def test_the_lock_is_released_when_a_run_fails_so_another_can_start(
        self, database: Database
    ) -> None:
        schema = schema_name()
        broken = [Migration(1, "broken", "SELECT 1/0")]

        async with database.connection() as first, database.connection() as second:
            with pytest.raises(asyncpg.DivisionByZeroError):
                await migrate(first, schema, broken)

            # A different session: it would wait forever if the first still held the lock.
            done = await asyncio.wait_for(migrate(second, schema, things(schema)), timeout=10)

        assert done == [1, 2, 3]


class TestTemporaryDatabase:
    async def test_it_has_the_vector_extension_and_is_gone_afterwards(self) -> None:
        async with temporary_database() as settings:
            connection = await asyncpg.connect(
                host=settings.postgres_host,
                port=settings.postgres_port,
                user=settings.postgres_user,
                password=settings.password(),
                database=settings.postgres_db,
            )
            try:
                extension = await connection.fetchval(
                    "SELECT extname FROM pg_extension WHERE extname = 'vector'"
                )
            finally:
                await connection.close()
            name = settings.postgres_db

        admin = await asyncpg.connect(
            host=settings.postgres_host,
            port=settings.postgres_port,
            user=settings.postgres_user,
            password=settings.password(),
            database="postgres",
        )
        try:
            still_there = await admin.fetchval(
                "SELECT count(*) FROM pg_database WHERE datname = $1", name
            )
        finally:
            await admin.close()
        assert extension == "vector"
        assert still_there == 0
