"""What is particular to Postgres: the index that makes a large collection fast, the way words are
read in each language, and that nothing crosses a tenant even in the SQL itself. The promises every
store makes are in ``test_knowledge_store_contract``."""

import asyncio
import math
from uuid import uuid4

import pytest
import pytest_asyncio

from factotum.contracts.errors import CollectionNotFoundError
from factotum.contracts.identity import Owner, OwnerKind
from factotum.contracts.ids import ChunkId, TenantId
from factotum.contracts.knowledge import Collection
from factotum.db.pool import Database
from factotum.knowledge.chunker import Child, Chunks, Parent
from factotum.knowledge.store import JobKind
from factotum.knowledge.store.postgres import (
    TEXT_SEARCH_SQL,
    PostgresStore,
    prepare,
    vector_index_name,
    vector_index_sql,
    vector_search_sql,
)
from factotum.knowledge.testing import (
    indexed,
    make_collection,
    new_document,
    pieces,
    wipe,
)

X = (1.0, 0.0, 0.0, 0.0)
Y = (0.0, 1.0, 0.0, 0.0)
Z = (0.0, 0.0, 1.0, 0.0)
W = (0.0, 0.0, 0.0, 1.0)


pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]


@pytest_asyncio.fixture(loop_scope="module")
async def pg(knowledge_database: Database) -> PostgresStore:
    """A store on an empty database. Its threshold is low, so that a few pieces make an index."""
    await wipe(knowledge_database)
    return PostgresStore(knowledge_database, index_threshold=3)


@pytest.fixture
def tenant() -> TenantId:
    return TenantId(uuid4())


@pytest.fixture
def owner() -> Owner:
    return Owner(kind=OwnerKind.USER, id=uuid4())


async def index_definition(pg: PostgresStore, collection: Collection) -> str | None:
    async with pg.database.connection() as connection:
        found = await connection.fetchval(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = 'knowledge' AND indexname = $1",
            vector_index_name(collection.id),
        )
    return None if found is None else str(found)


def basis(dimensions: int, place: int) -> tuple[float, ...]:
    return tuple(1.0 if n == place else 0.0 for n in range(dimensions))


async def grown(
    pg: PostgresStore,
    tenant: TenantId,
    owner: Owner,
    *,
    dimensions: int = 4,
    count: int = 6,
    name: str = "Manuals",
) -> tuple[Collection, list[ChunkId]]:
    """A collection with ``count`` pieces, each with a vector of its own direction."""
    collection = await make_collection(pg, tenant, owner, name, dimensions=dimensions)
    document = await pg.add_document(tenant, new_document(collection.id, name.encode()))
    ids = await indexed(
        pg,
        tenant,
        collection,
        document,
        pieces(*[f"piece number {n}" for n in range(count)]),
        [basis(dimensions, n) for n in range(count)],
    )
    return collection, ids


class TestTheIndex:
    async def test_a_small_collection_is_searched_exactly_and_has_no_index(
        self, pg: PostgresStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection, _ = await grown(pg, tenant, owner, count=2)

        await pg.optimize(tenant, collection.id)

        assert await index_definition(pg, collection) is None

    async def test_a_collection_that_has_grown_gets_an_index_for_its_size(
        self, pg: PostgresStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection, _ = await grown(pg, tenant, owner, dimensions=4, count=6)

        await pg.optimize(tenant, collection.id)

        definition = await index_definition(pg, collection)
        assert definition is not None
        assert "USING hnsw" in definition
        assert "(embedding)::vector(4)" in definition
        assert f"collection_id = '{collection.id}'" in definition

    async def test_optimizing_again_changes_nothing(
        self, pg: PostgresStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection, _ = await grown(pg, tenant, owner)
        await pg.optimize(tenant, collection.id)
        before = await index_definition(pg, collection)

        await pg.optimize(tenant, collection.id)

        assert await index_definition(pg, collection) == before

    async def test_pieces_without_a_vector_do_not_count_towards_it(
        self, pg: PostgresStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(pg, tenant, owner)
        document = await pg.add_document(tenant, new_document(collection.id))
        await indexed(pg, tenant, collection, document, pieces(*[f"p{n}" for n in range(10)]))

        await pg.optimize(tenant, collection.id)

        assert await index_definition(pg, collection) is None

    async def test_the_search_is_answered_by_the_index(
        self, pg: PostgresStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection, _ = await grown(pg, tenant, owner)
        await pg.optimize(tenant, collection.id)

        async with pg.database.transaction() as connection:
            # A table this small is read faster than indexed: ask for the index all the same, to
            # see that the query is written in a way it can answer.
            await connection.execute("SET LOCAL enable_seqscan = off")
            await connection.execute("SET LOCAL enable_sort = off")
            plan = await connection.fetchval(
                "EXPLAIN (FORMAT JSON) " + vector_search_sql(collection.id, 4),
                tenant,
                "[1,0,0,0]",
                5,
            )

        assert vector_index_name(collection.id) in str(plan)

    async def test_the_answers_are_the_same_with_the_index_as_without(
        self, pg: PostgresStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection, _ = await grown(pg, tenant, owner, dimensions=8, count=8)
        query = (0.9, 0.5, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0)
        exact = await pg.search_vector(tenant, collection.id, query, limit=5)

        await pg.optimize(tenant, collection.id)
        approximate = await pg.search_vector(tenant, collection.id, query, limit=5)

        assert [h.chunk_id for h in approximate] == [h.chunk_id for h in exact]
        assert [round(h.score, 6) for h in approximate] == [round(h.score, 6) for h in exact]

    async def test_the_index_goes_with_the_collection(
        self, pg: PostgresStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection, _ = await grown(pg, tenant, owner)
        await pg.optimize(tenant, collection.id)

        await pg.delete_collection(tenant, collection.id)

        assert await index_definition(pg, collection) is None

    async def test_a_collection_moved_to_another_space_gets_an_index_for_its_new_size(
        self, pg: PostgresStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection, _ = await grown(pg, tenant, owner, dimensions=4)
        await pg.optimize(tenant, collection.id)

        await pg.begin_reindex(tenant, collection.id, space_id="emb-two", dimensions=3)
        assert await index_definition(pg, collection) is None
        pending = await pg.pending_embeddings(tenant, collection.id, limit=100)
        await pg.store_embeddings(
            tenant,
            collection.id,
            [(p.chunk_id, basis(3, n % 3)) for n, p in enumerate(pending)],
        )
        await pg.finish_reindex(tenant, collection.id)
        await pg.optimize(tenant, collection.id)

        definition = await index_definition(pg, collection)
        assert definition is not None
        assert "(embedding)::vector(3)" in definition
        hits = await pg.search_vector(tenant, collection.id, basis(3, 1), limit=2)
        assert len(hits) == 2

    async def test_more_than_two_thousand_dimensions_are_indexed_in_half_precision(
        self, pg: PostgresStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection, ids = await grown(pg, tenant, owner, dimensions=2500, count=4)

        await pg.optimize(tenant, collection.id)

        definition = await index_definition(pg, collection)
        assert definition is not None
        assert "(embedding)::halfvec(2500)" in definition
        assert "halfvec_cosine_ops" in definition
        hits = await pg.search_vector(tenant, collection.id, basis(2500, 2), limit=1)
        assert [h.chunk_id for h in hits] == [ids[2]]

    async def test_vectors_too_large_for_any_index_are_searched_exactly(
        self, pg: PostgresStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection, ids = await grown(pg, tenant, owner, dimensions=4500, count=4)

        await pg.optimize(tenant, collection.id)

        assert await index_definition(pg, collection) is None
        hits = await pg.search_vector(tenant, collection.id, basis(4500, 1), limit=1)
        assert [h.chunk_id for h in hits] == [ids[1]]

    async def test_an_index_that_was_left_unusable_is_built_again(
        self, pg: PostgresStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection, _ = await grown(pg, tenant, owner)
        await pg.optimize(tenant, collection.id)
        async with pg.database.connection() as connection:
            # What a build that was interrupted leaves behind.
            await connection.execute(
                "UPDATE pg_index SET indisvalid = false WHERE indexrelid = $1::text::regclass",
                f"knowledge.{vector_index_name(collection.id)}",
            )

        await pg.optimize(tenant, collection.id)

        async with pg.database.connection() as connection:
            valid = await connection.fetchval(
                "SELECT indisvalid FROM pg_index WHERE indexrelid = $1::text::regclass",
                f"knowledge.{vector_index_name(collection.id)}",
            )
        assert valid is True

    async def test_two_builders_at_once_make_one_index(
        self, pg: PostgresStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection, _ = await grown(pg, tenant, owner)

        await asyncio.gather(*[pg.optimize(tenant, collection.id) for _ in range(4)])

        assert await index_definition(pg, collection) is not None

    async def test_a_builder_that_had_to_wait_for_another_finds_the_index_made(
        self, pg: PostgresStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection, _ = await grown(pg, tenant, owner)
        name = vector_index_name(collection.id)
        async with pg.database.connection() as holder:
            await holder.execute("SELECT pg_advisory_lock(hashtext($1))", name)
            waiting = asyncio.create_task(pg.optimize(tenant, collection.id))
            for _ in range(100):  # until it is really queued behind the lock
                async with pg.database.connection() as watcher:
                    queued = await watcher.fetchval(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
                    )
                if queued:
                    break
                await asyncio.sleep(0.05)
            async with pg.database.connection() as builder:
                await builder.execute(
                    vector_index_sql(collection.id, 4).replace(" CONCURRENTLY", "")
                )
            await holder.execute("SELECT pg_advisory_unlock(hashtext($1))", name)

            await waiting

        assert await index_definition(pg, collection) is not None

    async def test_the_index_of_one_collection_does_not_hold_another_one(
        self, pg: PostgresStore, tenant: TenantId, owner: Owner
    ) -> None:
        first, first_ids = await grown(pg, tenant, owner, count=5, name="First")
        second, second_ids = await grown(pg, tenant, owner, count=5, name="Second")
        await pg.optimize(tenant, first.id)
        await pg.optimize(tenant, second.id)

        hits = await pg.search_vector(tenant, first.id, X, limit=10)

        assert {h.chunk_id for h in hits} <= set(first_ids)
        assert not {h.chunk_id for h in hits} & set(second_ids)


class TestWordsInEachLanguage:
    async def in_language(
        self,
        pg: PostgresStore,
        tenant: TenantId,
        owner: Owner,
        collection: Collection,
        language: str | None,
        *texts: str,
        content: bytes | None = None,
    ) -> list[ChunkId]:
        document = await pg.add_document(
            tenant,
            new_document(
                collection.id, content or f"{language}{texts}".encode(), language=language
            ),
        )
        return await indexed(pg, tenant, collection, document, pieces(*texts))

    async def test_the_words_of_an_italian_document_are_stemmed_the_italian_way(
        self, pg: PostgresStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(pg, tenant, owner)
        ids = await self.in_language(pg, tenant, owner, collection, "it", "Le macchine sono ferme")

        hits = await pg.search_text(tenant, [collection.id], "macchina", limit=5)

        assert [h.chunk_id for h in hits] == ids

    async def test_the_words_of_an_english_document_are_stemmed_the_english_way(
        self, pg: PostgresStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(pg, tenant, owner)
        ids = await self.in_language(
            pg, tenant, owner, collection, "en", "The routers keep their configurations"
        )

        hits = await pg.search_text(tenant, [collection.id], "router configuration", limit=5)

        assert [h.chunk_id for h in hits] == ids

    async def test_stop_words_do_not_make_a_match(
        self, pg: PostgresStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(pg, tenant, owner)
        await self.in_language(pg, tenant, owner, collection, "en", "The cat is on the mat")

        assert await pg.search_text(tenant, [collection.id], "the of and is", limit=5) == []

    async def test_a_document_in_a_language_the_service_does_not_know_is_read_word_for_word(
        self, pg: PostgresStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(pg, tenant, owner)
        ids = await self.in_language(pg, tenant, owner, collection, None, "Routers everywhere")

        assert await pg.search_text(tenant, [collection.id], "router", limit=5) == []
        hits = await pg.search_text(tenant, [collection.id], "ROUTERS", limit=5)
        assert [h.chunk_id for h in hits] == ids

    async def test_each_document_of_a_collection_is_read_in_its_own_language(
        self, pg: PostgresStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(pg, tenant, owner)
        english = await self.in_language(
            pg, tenant, owner, collection, "en", "The cars are stopped"
        )
        italian = await self.in_language(
            pg, tenant, owner, collection, "it", "Le macchine sono ferme"
        )

        hits = await pg.search_text(tenant, [collection.id], "car macchina", limit=5)

        assert {h.chunk_id for h in hits} == {*english, *italian}
        assert {h.language for h in hits} == {"en", "it"}

    async def test_the_limit_holds_for_the_languages_together(
        self, pg: PostgresStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(pg, tenant, owner)
        await self.in_language(
            pg, tenant, owner, collection, "en", *[f"router {n}" for n in range(4)]
        )
        await self.in_language(
            pg, tenant, owner, collection, "it", *[f"router {n}" for n in range(4)]
        )

        hits = await pg.search_text(tenant, [collection.id], "router", limit=5)

        assert len(hits) == 5

    async def test_a_long_question_finds_the_piece_that_answers_it(
        self, pg: PostgresStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(pg, tenant, owner)
        ids = await self.in_language(
            pg,
            tenant,
            owner,
            collection,
            "en",
            "To restore the factory settings hold the reset button for ten seconds.",
            "The warranty covers manufacturing defects for two years.",
            "Cats sleep most of the day.",
        )

        hits = await pg.search_text(
            tenant,
            [collection.id],
            "How do I restore my router to the factory settings if I forgot the password?",
            limit=3,
        )

        assert hits[0].chunk_id == ids[0]

    async def test_a_piece_is_found_by_the_words_of_its_headings_but_they_weigh_less(
        self, pg: PostgresStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(pg, tenant, owner)
        document = await pg.add_document(tenant, new_document(collection.id, language="en"))
        both = Chunks(
            (
                Parent(0, "Notes about ports.", ("Firewall",), None),
                Parent(1, "The firewall blocks everything.", ("Networking",), None),
            ),
            (
                Child(0, 0, "Notes about ports.", ("Firewall",), None),
                Child(1, 1, "The firewall blocks everything.", ("Networking",), None),
            ),
        )
        in_heading, in_text = await indexed(pg, tenant, collection, document, both)

        hits = await pg.search_text(tenant, [collection.id], "firewall", limit=5)

        assert [h.chunk_id for h in hits] == [in_text, in_heading]


class TestNothingCrossesATenant:
    """The service checks the collection first; the SQL still says the tenant, so that a query that
    slipped through would find nothing, and so that row-level security has a column to read."""

    async def test_a_search_by_words_written_for_another_tenant_finds_nothing(
        self, pg: PostgresStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(pg, tenant, owner)
        document = await pg.add_document(tenant, new_document(collection.id, language="en"))
        await indexed(pg, tenant, collection, document, pieces("router password"))
        async with pg.database.connection() as connection:
            rows = await connection.fetch(
                TEXT_SEARCH_SQL,
                TenantId(uuid4()),
                [collection.id],
                "router",
                "knowledge.english",
                "knowledge.english",
                10,
            )
            same = await connection.fetch(
                TEXT_SEARCH_SQL,
                tenant,
                [collection.id],
                "router",
                "knowledge.english",
                "knowledge.english",
                10,
            )

        assert rows == []
        assert len(same) == 1

    async def test_a_search_by_meaning_written_for_another_tenant_finds_nothing(
        self, pg: PostgresStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection, _ = await grown(pg, tenant, owner)

        async with pg.database.connection() as connection:
            rows = await connection.fetch(
                vector_search_sql(collection.id, 4), TenantId(uuid4()), "[1,0,0,0]", 5
            )
            same = await connection.fetch(
                vector_search_sql(collection.id, 4), tenant, "[1,0,0,0]", 5
            )

        assert rows == []
        assert len(same) == 5

    async def test_every_table_says_which_tenant_a_row_belongs_to(self, pg: PostgresStore) -> None:
        async with pg.database.connection() as connection:
            tables = await connection.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'knowledge' AND table_type = 'BASE TABLE' "
                "AND table_name <> 'schema_migrations'"
            )
            with_tenant = await connection.fetch(
                "SELECT table_name FROM information_schema.columns "
                "WHERE table_schema = 'knowledge' AND column_name = 'tenant_id'"
            )

        assert {r["table_name"] for r in tables} == {r["table_name"] for r in with_tenant}
        assert len(tables) == 6

    async def test_the_collection_a_piece_is_in_is_the_collection_of_its_document(
        self, pg: PostgresStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection, _ = await grown(pg, tenant, owner)

        async with pg.database.connection() as connection:
            mismatched = await connection.fetchval(
                "SELECT count(*) FROM knowledge.chunks c JOIN knowledge.documents d "
                "ON d.id = c.document_id WHERE c.collection_id <> d.collection_id "
                "OR c.tenant_id <> d.tenant_id"
            )

        assert mismatched == 0
        assert (await pg.get_collection(tenant, collection.id)).chunk_count == 6


class TestJobs:
    async def test_workers_that_ask_together_get_different_jobs(
        self, pg: PostgresStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(pg, tenant, owner)
        for n in range(5):
            await pg.add_document(tenant, new_document(collection.id, f"doc{n}".encode()))

        claims = await asyncio.gather(
            *[pg.claim_job(f"worker-{n}", lease_seconds=60) for n in range(8)]
        )

        jobs = [job for job in claims if job is not None]
        assert len(jobs) == 5
        assert len({job.id for job in jobs}) == 5
        assert len({job.document_id for job in jobs}) == 5

    async def test_a_reindex_asked_for_twice_at_once_is_queued_once(
        self, pg: PostgresStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(pg, tenant, owner)

        await asyncio.gather(*[pg.enqueue_reindex(tenant, collection.id) for _ in range(5)])

        first = await pg.claim_job("worker", lease_seconds=60)
        assert first is not None
        assert first.kind is JobKind.REINDEX
        assert await pg.claim_job("worker", lease_seconds=60) is None

    async def test_a_document_that_is_refused_leaves_no_job_and_no_file_behind(
        self, pg: PostgresStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(pg, tenant, owner)
        await pg.add_document(tenant, new_document(collection.id, b"same"))

        with pytest.raises(Exception, match="already has"):
            await pg.add_document(tenant, new_document(collection.id, b"same"))

        async with pg.database.connection() as connection:
            jobs = await connection.fetchval("SELECT count(*) FROM knowledge.jobs")
            files = await connection.fetchval("SELECT count(*) FROM knowledge.document_data")
        assert (jobs, files) == (1, 1)

    async def test_deleting_a_collection_leaves_nothing_of_it_in_any_table(
        self, pg: PostgresStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection, _ = await grown(pg, tenant, owner)
        await pg.enqueue_reindex(tenant, collection.id)

        await pg.delete_collection(tenant, collection.id)

        async with pg.database.connection() as connection:
            for table in ("collections", "documents", "document_data", "parents", "chunks", "jobs"):
                assert await connection.fetchval(f"SELECT count(*) FROM knowledge.{table}") == 0  # noqa: S608


class TestTheTables:
    async def test_bringing_them_up_to_date_twice_does_nothing_the_second_time(
        self, pg: PostgresStore
    ) -> None:
        assert await prepare(pg.database) == []

    async def test_many_pieces_are_stored_in_one_go(
        self, pg: PostgresStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(pg, tenant, owner)
        document = await pg.add_document(tenant, new_document(collection.id, language="en"))

        await indexed(
            pg,
            tenant,
            collection,
            document,
            pieces(*[f"piece number {n} of many" for n in range(3000)]),
        )

        assert (await pg.get_document(tenant, document.id)).chunk_count == 3000
        hits = await pg.search_text(tenant, [collection.id], "number 2999", limit=1)
        assert [h.text for h in hits] == ["piece number 2999 of many"]

    async def test_a_number_that_is_not_finite_is_not_stored_as_a_vector(
        self, pg: PostgresStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(pg, tenant, owner)
        document = await pg.add_document(tenant, new_document(collection.id))
        await indexed(pg, tenant, collection, document, pieces("a"))
        [pending] = await pg.pending_embeddings(tenant, collection.id, limit=5)

        with pytest.raises(ValueError, match="finite"):
            await pg.store_embeddings(
                tenant, collection.id, [(pending.chunk_id, (math.nan, 0.0, 0.0, 0.0))]
            )

    async def test_the_collection_of_another_tenant_cannot_be_optimized(
        self, pg: PostgresStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection, _ = await grown(pg, tenant, owner)

        with pytest.raises(CollectionNotFoundError):
            await pg.optimize(TenantId(uuid4()), collection.id)
