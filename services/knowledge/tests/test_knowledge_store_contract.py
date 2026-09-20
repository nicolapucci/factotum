"""What every store promises. The same tests run against each implementation (see ``conftest``):
the one in memory, which the tests of everything above the database depend on, and Postgres, which
is what runs for real. Whatever they disagree on is a bug in one of them."""

import hashlib
from uuid import uuid4

import pytest
import pytest_asyncio

from factotum.contracts.errors import (
    CollectionNotFoundError,
    ConflictError,
    DocumentNotFoundError,
)
from factotum.contracts.identity import Owner, OwnerKind
from factotum.contracts.ids import ChunkId, CollectionId, DocumentId, TenantId
from factotum.contracts.knowledge import (
    Collection,
    CollectionStatus,
    CollectionUpdate,
    DocumentStatus,
)
from factotum.knowledge.ids import chunk_id
from factotum.knowledge.store import JobKind, KnowledgeStore
from factotum.knowledge.store.postgres import PostgresStore
from factotum.knowledge.testing import (
    family,
    indexed,
    make_collection,
    new_document,
    pieces,
    wipe,
)

pytestmark = pytest.mark.asyncio(loop_scope="module")

X = (1.0, 0.0, 0.0, 0.0)
Y = (0.0, 1.0, 0.0, 0.0)
Z = (0.0, 0.0, 1.0, 0.0)
XY = (1.0, 1.0, 0.0, 0.0)
W = (0.0, 0.0, 0.0, 1.0)


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def start_empty(store: KnowledgeStore) -> None:
    """The database of Postgres lasts as long as the module: each test starts from nothing."""
    if isinstance(store, PostgresStore):
        await wipe(store.database)


@pytest.fixture
def tenant() -> TenantId:
    return TenantId(uuid4())


@pytest.fixture
def owner() -> Owner:
    return Owner(kind=OwnerKind.USER, id=uuid4())


class TestCollections:
    async def test_a_new_collection_is_empty_and_active(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await store.create_collection(
            tenant, owner, name="Manuals", description="Devices", space_id="emb-x", dimensions=4
        )

        assert collection.tenant_id == tenant
        assert collection.owner == owner
        assert (collection.name, collection.description) == ("Manuals", "Devices")
        assert (collection.space_id, collection.dimensions) == ("emb-x", 4)
        assert collection.status is CollectionStatus.ACTIVE
        assert (collection.document_count, collection.chunk_count) == (0, 0)
        assert collection.languages == ()
        assert collection.created_at.tzinfo is not None

    async def test_it_can_be_read_again(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        created = await make_collection(store, tenant, owner)

        assert await store.get_collection(tenant, created.id) == created

    async def test_one_that_does_not_exist_is_not_found(
        self, store: KnowledgeStore, tenant: TenantId
    ) -> None:
        with pytest.raises(CollectionNotFoundError):
            await store.get_collection(tenant, CollectionId(uuid4()))

    async def test_a_tenant_lists_its_own_collections_oldest_first(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        first = await make_collection(store, tenant, owner, "First")
        second = await make_collection(store, tenant, owner, "Second")
        await make_collection(store, TenantId(uuid4()), owner, "Somebody else's")

        assert [c.id for c in await store.list_collections(tenant)] == [first.id, second.id]

    async def test_it_can_be_renamed_and_described(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)

        updated = await store.update_collection(
            tenant, collection.id, CollectionUpdate(name="Guides", description="How-tos")
        )

        assert (updated.name, updated.description) == ("Guides", "How-tos")
        assert updated.updated_at >= collection.updated_at
        assert await store.get_collection(tenant, collection.id) == updated

    async def test_changing_only_the_description_keeps_the_name(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)

        updated = await store.update_collection(
            tenant, collection.id, CollectionUpdate(description="New")
        )

        assert (updated.name, updated.description) == ("Manuals", "New")

    async def test_the_same_owner_cannot_have_two_collections_of_one_name(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        await make_collection(store, tenant, owner, "Manuals")

        with pytest.raises(ConflictError):
            await make_collection(store, tenant, owner, "MANUALS")

    async def test_another_owner_may_use_the_same_name(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        await make_collection(store, tenant, owner, "Manuals")
        other = Owner(kind=OwnerKind.TENANT, id=tenant)

        assert (await make_collection(store, tenant, other, "Manuals")).owner == other

    async def test_renaming_to_a_name_that_is_taken_is_a_conflict(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        await make_collection(store, tenant, owner, "Manuals")
        other = await make_collection(store, tenant, owner, "Guides")

        with pytest.raises(ConflictError):
            await store.update_collection(tenant, other.id, CollectionUpdate(name="manuals"))

    async def test_renaming_to_its_own_name_is_fine(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner, "Manuals")

        updated = await store.update_collection(
            tenant, collection.id, CollectionUpdate(name="Manuals")
        )

        assert updated.name == "Manuals"

    async def test_a_deleted_collection_takes_its_documents_and_jobs_with_it(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        document = await store.add_document(tenant, new_document(collection.id))
        await indexed(store, tenant, collection, document, pieces("router"), [X])

        await store.delete_collection(tenant, collection.id)

        with pytest.raises(CollectionNotFoundError):
            await store.get_collection(tenant, collection.id)
        with pytest.raises(DocumentNotFoundError):
            await store.get_document(tenant, document.id)
        assert await store.claim_job("worker", lease_seconds=60) is None

    async def test_the_name_of_a_deleted_collection_is_free_again(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner, "Manuals")
        await store.delete_collection(tenant, collection.id)

        assert (await make_collection(store, tenant, owner, "Manuals")).name == "Manuals"

    async def test_another_tenant_cannot_see_touch_or_delete_it(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        stranger = TenantId(uuid4())

        with pytest.raises(CollectionNotFoundError):
            await store.get_collection(stranger, collection.id)
        with pytest.raises(CollectionNotFoundError):
            await store.update_collection(stranger, collection.id, CollectionUpdate(name="Mine"))
        with pytest.raises(CollectionNotFoundError):
            await store.delete_collection(stranger, collection.id)
        assert await store.list_collections(stranger) == []
        assert (await store.get_collection(tenant, collection.id)).name == "Manuals"

    async def test_the_counts_follow_its_documents_and_pieces(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        one = await store.add_document(tenant, new_document(collection.id, b"one"))
        two = await store.add_document(tenant, new_document(collection.id, b"two"))
        await indexed(store, tenant, collection, one, pieces("a", "b"))
        await indexed(store, tenant, collection, two, pieces("c"))

        counted = await store.get_collection(tenant, collection.id)

        assert (counted.document_count, counted.chunk_count) == (2, 3)

    async def test_its_languages_are_those_of_its_documents(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        await store.add_document(tenant, new_document(collection.id, b"en", language="en"))
        await store.add_document(tenant, new_document(collection.id, b"it", language="it"))
        await store.add_document(tenant, new_document(collection.id, b"en2", language="en"))
        await store.add_document(tenant, new_document(collection.id, b"none"))

        assert (await store.get_collection(tenant, collection.id)).languages == ("en", "it")

    async def test_a_deleted_collection_takes_its_queued_reindex_with_it(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        await store.enqueue_reindex(tenant, collection.id)

        await store.delete_collection(tenant, collection.id)

        assert await store.claim_job("worker", lease_seconds=60) is None


class TestReindexing:
    async def test_a_collection_is_marked_when_its_vectors_are_out_of_date(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)

        await store.mark_needs_reindex(tenant, collection.id)
        await store.mark_needs_reindex(tenant, collection.id)

        assert (await store.get_collection(tenant, collection.id)).status is (
            CollectionStatus.NEEDS_REINDEX
        )

    async def test_a_collection_being_reindexed_can_be_marked_again_if_the_model_changes_once_more(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        await store.begin_reindex(tenant, collection.id, space_id="emb-two", dimensions=4)

        await store.mark_needs_reindex(tenant, collection.id)

        assert (await store.get_collection(tenant, collection.id)).status is (
            CollectionStatus.NEEDS_REINDEX
        )

    async def test_reindexing_moves_it_to_the_new_space_and_forgets_the_old_vectors(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        document = await store.add_document(tenant, new_document(collection.id))
        await indexed(store, tenant, collection, document, pieces("router"), [X])

        moved = await store.begin_reindex(tenant, collection.id, space_id="emb-two", dimensions=3)

        assert (moved.space_id, moved.dimensions) == ("emb-two", 3)
        assert moved.status is CollectionStatus.REINDEXING
        assert (await store.get_document(tenant, document.id)).chunks_embedded == 0
        assert len(await store.pending_embeddings(tenant, collection.id, limit=10)) == 1

    async def test_the_new_space_takes_vectors_of_its_own_size(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        document = await store.add_document(tenant, new_document(collection.id))
        await indexed(store, tenant, collection, document, pieces("router"), [X])
        await store.begin_reindex(tenant, collection.id, space_id="emb-two", dimensions=3)
        [pending] = await store.pending_embeddings(tenant, collection.id, limit=10)

        await store.store_embeddings(tenant, collection.id, [(pending.chunk_id, (0.0, 1.0, 0.0))])

        hits = await store.search_vector(tenant, collection.id, (0.0, 1.0, 0.0), limit=5)
        assert [h.chunk_id for h in hits] == [pending.chunk_id]

    async def test_beginning_again_for_the_same_space_keeps_the_work_done(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        document = await store.add_document(tenant, new_document(collection.id))
        await indexed(store, tenant, collection, document, pieces("router", "switch"))
        await store.begin_reindex(tenant, collection.id, space_id="emb-two", dimensions=4)
        [first, _] = await store.pending_embeddings(tenant, collection.id, limit=10)
        await store.store_embeddings(tenant, collection.id, [(first.chunk_id, X)])

        await store.begin_reindex(tenant, collection.id, space_id="emb-two", dimensions=4)

        assert (await store.get_document(tenant, document.id)).chunks_embedded == 1

    async def test_finishing_makes_it_active_again(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        await store.begin_reindex(tenant, collection.id, space_id="emb-two", dimensions=4)

        finished = await store.finish_reindex(tenant, collection.id)

        assert finished.status is CollectionStatus.ACTIVE

    async def test_another_tenant_cannot_reindex_it(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)

        with pytest.raises(CollectionNotFoundError):
            await store.begin_reindex(
                TenantId(uuid4()), collection.id, space_id="emb-two", dimensions=4
            )
        with pytest.raises(CollectionNotFoundError):
            await store.mark_needs_reindex(TenantId(uuid4()), collection.id)

    async def test_reindexing_one_collection_leaves_the_vectors_of_another_alone(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        moving = await make_collection(store, tenant, owner, "Moving")
        staying = await make_collection(store, tenant, owner, "Staying")
        staying_document = await store.add_document(tenant, new_document(staying.id))
        await indexed(store, tenant, staying, staying_document, pieces("router"), [X])

        await store.begin_reindex(tenant, moving.id, space_id="emb-two", dimensions=3)

        assert (await store.get_document(tenant, staying_document.id)).chunks_embedded == 1

    async def test_optimizing_changes_no_answer(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        document = await store.add_document(tenant, new_document(collection.id))
        await indexed(
            store, tenant, collection, document, pieces("a", "b", "c", "d", "e"), [X, Y, Z, XY, W]
        )
        before = await store.search_vector(tenant, collection.id, X, limit=5)

        await store.optimize(tenant, collection.id)
        await store.optimize(tenant, collection.id)

        assert await store.search_vector(tenant, collection.id, X, limit=5) == before

    async def test_another_tenant_cannot_optimize_it(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)

        with pytest.raises(CollectionNotFoundError):
            await store.optimize(TenantId(uuid4()), collection.id)


class TestDocuments:
    async def test_a_new_document_is_waiting_to_be_read(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)

        document = await store.add_document(
            tenant, new_document(collection.id, b"the file", title="Guide", source="guide.pdf")
        )

        assert document.collection_id == collection.id
        assert (document.title, document.source) == ("Guide", "guide.pdf")
        assert document.media_type == "application/pdf"
        assert document.size_bytes == len(b"the file")
        assert document.content_sha256 == hashlib.sha256(b"the file").hexdigest()
        assert document.status is DocumentStatus.PENDING
        assert (document.chunk_count, document.chunks_embedded) == (0, 0)
        assert document.error_code is None
        assert document.progress == 0.0

    async def test_the_file_is_kept_byte_for_byte(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        content = bytes(range(256)) * 40 + b"\x00\x00end"

        document = await store.add_document(tenant, new_document(collection.id, content))

        assert await store.document_data(tenant, document.id) == content

    async def test_it_goes_in_a_collection_that_exists(
        self, store: KnowledgeStore, tenant: TenantId
    ) -> None:
        with pytest.raises(CollectionNotFoundError):
            await store.add_document(tenant, new_document(CollectionId(uuid4())))

    async def test_the_same_content_is_not_kept_twice_in_a_collection(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        await store.add_document(tenant, new_document(collection.id, b"same", title="One"))

        with pytest.raises(ConflictError):
            await store.add_document(tenant, new_document(collection.id, b"same", title="Two"))

    async def test_the_same_content_may_be_in_two_collections(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        first = await make_collection(store, tenant, owner, "First")
        second = await make_collection(store, tenant, owner, "Second")

        await store.add_document(tenant, new_document(first.id, b"same"))
        await store.add_document(tenant, new_document(second.id, b"same"))

    async def test_adding_it_queues_it_to_be_read(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        document = await store.add_document(tenant, new_document(collection.id))

        job = await store.claim_job("worker", lease_seconds=60)

        assert job is not None
        assert (job.kind, job.tenant_id) == (JobKind.INGEST, tenant)
        assert (job.document_id, job.collection_id) == (document.id, collection.id)

    async def test_documents_are_listed_newest_first_and_in_pages(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        added = [
            await store.add_document(tenant, new_document(collection.id, f"doc{n}".encode()))
            for n in range(5)
        ]
        newest_first = [d.id for d in reversed(added)]

        assert [d.id for d in await store.list_documents(tenant, collection.id)] == newest_first
        page = await store.list_documents(tenant, collection.id, limit=2, offset=1)
        assert [d.id for d in page] == newest_first[1:3]

    async def test_only_the_documents_of_that_collection_are_listed(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        first = await make_collection(store, tenant, owner, "First")
        second = await make_collection(store, tenant, owner, "Second")
        mine = await store.add_document(tenant, new_document(first.id))
        await store.add_document(tenant, new_document(second.id))

        assert [d.id for d in await store.list_documents(tenant, first.id)] == [mine.id]

    async def test_a_document_that_does_not_exist_is_not_found(
        self, store: KnowledgeStore, tenant: TenantId
    ) -> None:
        for read in (store.get_document, store.document_data, store.delete_document):
            with pytest.raises(DocumentNotFoundError):
                await read(tenant, DocumentId(uuid4()))

    async def test_another_tenant_cannot_see_or_touch_it(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        document = await store.add_document(tenant, new_document(collection.id))
        stranger = TenantId(uuid4())

        with pytest.raises(DocumentNotFoundError):
            await store.get_document(stranger, document.id)
        with pytest.raises(DocumentNotFoundError):
            await store.document_data(stranger, document.id)
        with pytest.raises(DocumentNotFoundError):
            await store.delete_document(stranger, document.id)
        with pytest.raises(DocumentNotFoundError):
            await store.set_document_state(stranger, document.id, DocumentStatus.READY)
        with pytest.raises(DocumentNotFoundError):
            await store.replace_chunks(stranger, document.id, pieces("x"))
        with pytest.raises(CollectionNotFoundError):
            await store.list_documents(stranger, collection.id)
        assert (await store.get_document(tenant, document.id)).status is DocumentStatus.PENDING

    async def test_a_document_moves_from_pending_to_ready(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        document = await store.add_document(tenant, new_document(collection.id))

        await store.set_document_state(tenant, document.id, DocumentStatus.PROCESSING)
        assert (await store.get_document(tenant, document.id)).status is DocumentStatus.PROCESSING
        await store.set_document_state(tenant, document.id, DocumentStatus.READY)

        ready = await store.get_document(tenant, document.id)
        assert ready.status is DocumentStatus.READY
        assert ready.progress == 1.0

    async def test_a_failure_keeps_its_reason_until_the_document_recovers(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        document = await store.add_document(tenant, new_document(collection.id))

        await store.set_document_state(
            tenant,
            document.id,
            DocumentStatus.FAILED,
            error_code="unsupported_document",
            error_message="the PDF has no text",
        )
        failed = await store.get_document(tenant, document.id)
        assert failed.status is DocumentStatus.FAILED
        assert (failed.error_code, failed.error_message) == (
            "unsupported_document",
            "the PDF has no text",
        )

        await store.set_document_state(tenant, document.id, DocumentStatus.PROCESSING)
        again = await store.get_document(tenant, document.id)
        assert (again.error_code, again.error_message) == (None, None)

    async def test_a_detected_language_is_recorded(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        document = await store.add_document(tenant, new_document(collection.id))

        await store.set_document_state(
            tenant, document.id, DocumentStatus.PROCESSING, language="it"
        )

        detected = await store.get_document(tenant, document.id)
        assert (detected.language, detected.language_declared) == ("it", False)

    async def test_a_detected_language_never_replaces_a_declared_one(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        document = await store.add_document(
            tenant, new_document(collection.id, language="de", declared=True)
        )

        await store.set_document_state(
            tenant, document.id, DocumentStatus.PROCESSING, language="it"
        )

        kept = await store.get_document(tenant, document.id)
        assert (kept.language, kept.language_declared) == ("de", True)

    async def test_deleting_a_document_takes_its_pieces_and_its_job(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        gone = await store.add_document(tenant, new_document(collection.id, b"gone"))
        kept = await store.add_document(tenant, new_document(collection.id, b"kept"))
        await indexed(store, tenant, collection, gone, pieces("router alpha"), [X])
        await indexed(store, tenant, collection, kept, pieces("router beta"), [X])

        await store.delete_document(tenant, gone.id)

        with pytest.raises(DocumentNotFoundError):
            await store.get_document(tenant, gone.id)
        by_meaning = await store.search_vector(tenant, collection.id, X, limit=10)
        by_words = await store.search_text(tenant, [collection.id], "router", limit=10)
        assert [h.document_id for h in by_meaning] == [kept.id]
        assert [h.document_id for h in by_words] == [kept.id]
        claimed = await store.claim_job("worker", lease_seconds=60)
        assert claimed is not None
        assert claimed.document_id == kept.id
        assert await store.claim_job("worker", lease_seconds=60) is None


class TestJobs:
    async def test_there_is_nothing_to_claim_when_nothing_is_queued(
        self, store: KnowledgeStore
    ) -> None:
        assert await store.claim_job("worker", lease_seconds=60) is None

    async def test_jobs_are_claimed_oldest_first_whatever_the_tenant(
        self, store: KnowledgeStore, owner: Owner
    ) -> None:
        first_tenant, second_tenant = TenantId(uuid4()), TenantId(uuid4())
        one = await make_collection(store, first_tenant, owner)
        two = await make_collection(store, second_tenant, owner)
        a = await store.add_document(first_tenant, new_document(one.id, b"a"))
        b = await store.add_document(second_tenant, new_document(two.id, b"b"))
        c = await store.add_document(first_tenant, new_document(one.id, b"c"))

        claimed = [await store.claim_job("worker", lease_seconds=60) for _ in range(3)]

        assert [j.document_id for j in claimed if j] == [a.id, b.id, c.id]
        assert [j.tenant_id for j in claimed if j] == [first_tenant, second_tenant, first_tenant]

    async def test_a_job_that_is_being_worked_on_is_not_given_to_another_worker(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        await store.add_document(tenant, new_document(collection.id))

        mine = await store.claim_job("one", lease_seconds=3600)

        assert mine is not None
        assert mine.attempts == 1
        assert await store.claim_job("two", lease_seconds=3600) is None

    async def test_a_job_whose_worker_stopped_answering_is_given_to_another(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        await store.add_document(tenant, new_document(collection.id))
        first = await store.claim_job("one", lease_seconds=0)

        second = await store.claim_job("two", lease_seconds=3600)

        assert first is not None
        assert second is not None
        assert second.id == first.id
        assert second.attempts == 2

    async def test_a_finished_job_is_gone(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        await store.add_document(tenant, new_document(collection.id))
        job = await store.claim_job("worker", lease_seconds=0)
        assert job is not None

        await store.finish_job(job)

        assert await store.claim_job("worker", lease_seconds=60) is None

    async def test_a_worker_that_is_still_at_it_keeps_its_job(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        await store.add_document(tenant, new_document(collection.id))
        job = await store.claim_job("worker", lease_seconds=0)
        assert job is not None

        await store.extend_job(job, lease_seconds=3600)

        assert await store.claim_job("other", lease_seconds=60) is None

    async def test_a_worker_that_lost_its_claim_cannot_keep_the_job_going(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        await store.add_document(tenant, new_document(collection.id))
        slow = await store.claim_job("slow", lease_seconds=0)
        current = await store.claim_job("current", lease_seconds=0)
        assert slow is not None
        assert current is not None

        await store.extend_job(slow, lease_seconds=3600)

        assert await store.claim_job("third", lease_seconds=60) is not None

    async def test_a_worker_that_lost_its_claim_cannot_finish_the_job_of_the_one_who_has_it(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        await store.add_document(tenant, new_document(collection.id))
        slow = await store.claim_job("slow", lease_seconds=0)
        current = await store.claim_job("current", lease_seconds=0)
        assert slow is not None
        assert current is not None

        await store.finish_job(slow)
        await store.retry_job(slow, delay_seconds=3600)
        await store.fail_job(slow, "late")

        assert await store.claim_job("third", lease_seconds=60) is not None

    async def test_a_job_can_be_put_back_for_later(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        await store.add_document(tenant, new_document(collection.id))
        job = await store.claim_job("worker", lease_seconds=60)
        assert job is not None

        await store.retry_job(job, delay_seconds=3600)

        assert await store.claim_job("worker", lease_seconds=60) is None

    async def test_a_job_put_back_without_delay_is_claimed_again(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        await store.add_document(tenant, new_document(collection.id))
        job = await store.claim_job("worker", lease_seconds=3600)
        assert job is not None

        await store.retry_job(job, delay_seconds=0)

        again = await store.claim_job("worker", lease_seconds=60)
        assert again is not None
        assert (again.id, again.attempts) == (job.id, 2)

    async def test_a_failed_job_is_not_claimed_again(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        await store.add_document(tenant, new_document(collection.id))
        job = await store.claim_job("worker", lease_seconds=0)
        assert job is not None

        await store.fail_job(job, "it did not work")

        assert await store.claim_job("worker", lease_seconds=60) is None

    async def test_a_reindex_is_queued_once_until_it_is_done(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)

        await store.enqueue_reindex(tenant, collection.id)
        await store.enqueue_reindex(tenant, collection.id)
        job = await store.claim_job("worker", lease_seconds=0)

        assert job is not None
        assert (job.kind, job.collection_id, job.document_id) == (
            JobKind.REINDEX,
            collection.id,
            None,
        )
        await store.enqueue_reindex(tenant, collection.id)
        await store.finish_job(job)
        await store.enqueue_reindex(tenant, collection.id)
        assert await store.claim_job("worker", lease_seconds=60) is not None

    async def test_only_the_owner_tenant_can_queue_a_reindex(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)

        with pytest.raises(CollectionNotFoundError):
            await store.enqueue_reindex(TenantId(uuid4()), collection.id)


class TestPieces:
    async def test_a_document_has_the_pieces_it_was_given(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        document = await store.add_document(tenant, new_document(collection.id))

        await store.replace_chunks(tenant, document.id, pieces("one", "two", "three"))

        counted = await store.get_document(tenant, document.id)
        assert (counted.chunk_count, counted.chunks_embedded) == (3, 0)

    async def test_their_ids_follow_from_the_document_and_their_place_in_it(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        document = await store.add_document(tenant, new_document(collection.id))
        await store.replace_chunks(tenant, document.id, pieces("one", "two"))
        first = await store.pending_embeddings(tenant, collection.id, limit=10)

        await store.replace_chunks(tenant, document.id, pieces("one again", "two again"))
        second = await store.pending_embeddings(tenant, collection.id, limit=10)

        assert [p.chunk_id for p in first] == [p.chunk_id for p in second]
        assert [p.chunk_id for p in first] == [chunk_id(document.id, 0), chunk_id(document.id, 1)]

    async def test_new_pieces_replace_the_old_ones_and_their_vectors(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        document = await store.add_document(tenant, new_document(collection.id))
        await indexed(store, tenant, collection, document, pieces("old one", "old two"), [X, Y])

        await store.replace_chunks(tenant, document.id, pieces("fresh"))

        assert (await store.get_document(tenant, document.id)).chunk_count == 1
        assert await store.search_vector(tenant, collection.id, X, limit=10) == []
        assert await store.search_text(tenant, [collection.id], "old", limit=10) == []
        assert [
            p.text for p in await store.pending_embeddings(tenant, collection.id, limit=10)
        ] == ["fresh"]

    async def test_what_waits_to_be_embedded_comes_in_the_order_of_the_document(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        first = await store.add_document(tenant, new_document(collection.id, b"first"))
        second = await store.add_document(tenant, new_document(collection.id, b"second"))
        await store.replace_chunks(tenant, second.id, pieces("s0", "s1"))
        await store.replace_chunks(
            tenant, first.id, pieces("f0", "f1", "f2", headings=("Chapter",))
        )

        pending = await store.pending_embeddings(tenant, collection.id, limit=10)

        assert [(p.document_id, p.text) for p in pending] == [
            (first.id, "f0"),
            (first.id, "f1"),
            (first.id, "f2"),
            (second.id, "s0"),
            (second.id, "s1"),
        ]
        assert pending[0].headings == ("Chapter",)

    async def test_the_pieces_waiting_can_be_taken_a_batch_at_a_time(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        document = await store.add_document(tenant, new_document(collection.id))
        await store.replace_chunks(tenant, document.id, pieces(*[f"piece {n}" for n in range(5)]))

        batch = await store.pending_embeddings(tenant, collection.id, limit=2)
        await store.store_embeddings(tenant, collection.id, [(p.chunk_id, X) for p in batch])
        rest = await store.pending_embeddings(tenant, collection.id, limit=10)

        assert [p.text for p in batch] == ["piece 0", "piece 1"]
        assert [p.text for p in rest] == ["piece 2", "piece 3", "piece 4"]
        assert (await store.get_document(tenant, document.id)).chunks_embedded == 2

    async def test_the_pieces_waiting_can_be_asked_for_one_document_at_a_time(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        first = await store.add_document(tenant, new_document(collection.id, b"first"))
        second = await store.add_document(tenant, new_document(collection.id, b"second"))
        await store.replace_chunks(tenant, first.id, pieces("f0", "f1"))
        await store.replace_chunks(tenant, second.id, pieces("s0"))

        of_second = await store.pending_embeddings(
            tenant, collection.id, limit=10, document_id=second.id
        )

        assert [(p.document_id, p.text) for p in of_second] == [(second.id, "s0")]

    async def test_a_vector_of_the_wrong_size_is_refused_and_nothing_is_stored(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner, dimensions=4)
        document = await store.add_document(tenant, new_document(collection.id))
        await store.replace_chunks(tenant, document.id, pieces("a", "b"))
        first, second = await store.pending_embeddings(tenant, collection.id, limit=10)

        with pytest.raises(ValueError, match="does not fit"):
            await store.store_embeddings(
                tenant, collection.id, [(first.chunk_id, X), (second.chunk_id, (1.0, 2.0))]
            )

        assert (await store.get_document(tenant, document.id)).chunks_embedded == 0

    async def test_a_piece_that_is_gone_is_skipped(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        document = await store.add_document(tenant, new_document(collection.id))
        await store.replace_chunks(tenant, document.id, pieces("a"))
        [pending] = await store.pending_embeddings(tenant, collection.id, limit=10)

        await store.store_embeddings(
            tenant, collection.id, [(ChunkId(uuid4()), X), (pending.chunk_id, Y)]
        )

        assert (await store.get_document(tenant, document.id)).chunks_embedded == 1

    async def test_a_piece_of_another_collection_is_not_touched(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        mine = await make_collection(store, tenant, owner, "Mine")
        theirs = await make_collection(store, tenant, owner, "Theirs")
        document = await store.add_document(tenant, new_document(theirs.id))
        await store.replace_chunks(tenant, document.id, pieces("a"))
        [pending] = await store.pending_embeddings(tenant, theirs.id, limit=10)

        await store.store_embeddings(tenant, mine.id, [(pending.chunk_id, X)])

        assert (await store.get_document(tenant, document.id)).chunks_embedded == 0

    async def test_another_tenant_cannot_list_or_fill_them(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        stranger = TenantId(uuid4())

        with pytest.raises(CollectionNotFoundError):
            await store.pending_embeddings(stranger, collection.id, limit=10)
        with pytest.raises(CollectionNotFoundError):
            await store.store_embeddings(stranger, collection.id, [])

    async def test_storing_no_vectors_is_fine(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)

        await store.store_embeddings(tenant, collection.id, [])


class TestSearchByMeaning:
    async def test_the_nearest_piece_comes_first(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        document = await store.add_document(tenant, new_document(collection.id))
        ids = await indexed(
            store,
            tenant,
            collection,
            document,
            pieces("about x", "about xy", "about z"),
            [X, XY, Z],
        )

        hits = await store.search_vector(tenant, collection.id, X, limit=10)

        assert [h.chunk_id for h in hits] == [ids[0], ids[1], ids[2]]
        assert hits[0].score > hits[1].score > hits[2].score

    async def test_it_gives_what_is_needed_to_show_a_piece(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        document = await store.add_document(
            tenant,
            new_document(collection.id, title="Router manual", source="router.pdf", language="en"),
        )
        await indexed(
            store,
            tenant,
            collection,
            document,
            family(
                "The whole section about resetting.",
                "Hold the button.",
                headings=("Resetting", "Factory"),
                page=12,
            ),
            [X],
        )

        [hit] = await store.search_vector(tenant, collection.id, X, limit=1)

        assert hit.text == "Hold the button."
        assert hit.context == "The whole section about resetting."
        assert hit.headings == ("Resetting", "Factory")
        assert hit.page == 12
        assert (hit.document_id, hit.collection_id) == (document.id, collection.id)
        assert (hit.document_title, hit.source, hit.language) == (
            "Router manual",
            "router.pdf",
            "en",
        )

    async def test_pieces_of_one_section_share_a_parent(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        document = await store.add_document(tenant, new_document(collection.id))
        await indexed(
            store, tenant, collection, document, family("Section.", "one", "two"), [X, XY]
        )

        first, second = await store.search_vector(tenant, collection.id, X, limit=10)

        assert first.parent_id == second.parent_id

    async def test_the_limit_is_respected(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        document = await store.add_document(tenant, new_document(collection.id))
        await indexed(store, tenant, collection, document, pieces("a", "b", "c"), [X, XY, Z])

        assert len(await store.search_vector(tenant, collection.id, X, limit=2)) == 2

    async def test_a_piece_without_a_vector_is_not_found_by_meaning(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        document = await store.add_document(tenant, new_document(collection.id))
        await indexed(store, tenant, collection, document, pieces("no vector yet"))

        assert await store.search_vector(tenant, collection.id, X, limit=10) == []

    async def test_only_the_collection_asked_is_searched(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        mine = await make_collection(store, tenant, owner, "Mine")
        theirs = await make_collection(store, tenant, owner, "Theirs")
        mine_doc = await store.add_document(tenant, new_document(mine.id, b"m"))
        theirs_doc = await store.add_document(tenant, new_document(theirs.id, b"t"))
        mine_ids = await indexed(store, tenant, mine, mine_doc, pieces("mine"), [X])
        await indexed(store, tenant, theirs, theirs_doc, pieces("theirs"), [X])

        hits = await store.search_vector(tenant, mine.id, X, limit=10)

        assert [h.chunk_id for h in hits] == mine_ids

    async def test_a_query_of_the_wrong_size_is_refused(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner, dimensions=4)

        with pytest.raises(ValueError, match="does not fit"):
            await store.search_vector(tenant, collection.id, (1.0, 2.0), limit=10)

    async def test_another_tenant_finds_nothing_in_it(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)

        with pytest.raises(CollectionNotFoundError):
            await store.search_vector(TenantId(uuid4()), collection.id, X, limit=10)


class TestSearchByWords:
    async def seeded(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> tuple[Collection, list[ChunkId]]:
        collection = await make_collection(store, tenant, owner)
        document = await store.add_document(tenant, new_document(collection.id))
        ids = await indexed(
            store,
            tenant,
            collection,
            document,
            pieces(
                "The zebra router keeps its configuration in a file.",
                "The router boots when the power comes back.",
                "Cats sleep most of the day.",
            ),
        )
        return collection, ids

    async def test_pieces_that_have_the_words_are_found(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection, ids = await self.seeded(store, tenant, owner)

        hits = await store.search_text(tenant, [collection.id], "cats", limit=10)

        assert [h.chunk_id for h in hits] == [ids[2]]

    async def test_the_piece_with_more_of_the_words_comes_first(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection, ids = await self.seeded(store, tenant, owner)

        hits = await store.search_text(tenant, [collection.id], "zebra router", limit=10)

        assert [h.chunk_id for h in hits] == [ids[0], ids[1]]
        assert hits[0].score > hits[1].score

    async def test_a_question_finds_what_a_few_of_its_words_match(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection, ids = await self.seeded(store, tenant, owner)

        hits = await store.search_text(
            tenant, [collection.id], "how does the zebra router keep something", limit=10
        )

        assert ids[0] == hits[0].chunk_id

    async def test_words_that_are_nowhere_find_nothing(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection, _ = await self.seeded(store, tenant, owner)

        assert await store.search_text(tenant, [collection.id], "submarine", limit=10) == []

    async def test_a_query_of_nothing_but_symbols_finds_nothing_and_does_not_fail(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection, _ = await self.seeded(store, tenant, owner)

        for query in ("?!", "'; DROP TABLE chunks; --", "& | ! ( ) :*", "\\", "   "):
            assert await store.search_text(tenant, [collection.id], query, limit=10) == []

    async def test_accents_and_capitals_do_not_matter(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        document = await store.add_document(tenant, new_document(collection.id))
        ids = await indexed(store, tenant, collection, document, pieces("La città è bellissima"))

        hits = await store.search_text(tenant, [collection.id], "CITTA", limit=10)

        assert [h.chunk_id for h in hits] == ids

    async def test_the_headings_of_a_piece_are_searched_too(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        document = await store.add_document(tenant, new_document(collection.id))
        ids = await indexed(
            store,
            tenant,
            collection,
            document,
            pieces("Hold the button for ten seconds.", headings=("Troubleshooting", "Firewall")),
        )

        hits = await store.search_text(tenant, [collection.id], "firewall", limit=10)

        assert [h.chunk_id for h in hits] == ids

    async def test_it_gives_what_is_needed_to_show_a_piece(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        document = await store.add_document(
            tenant, new_document(collection.id, title="Cats", source="cats.txt", language="en")
        )
        await indexed(
            store,
            tenant,
            collection,
            document,
            family("All about cats.", "Cats sleep a lot.", headings=("Habits",), page=3),
        )

        [hit] = await store.search_text(tenant, [collection.id], "sleep", limit=10)

        assert (hit.text, hit.context) == ("Cats sleep a lot.", "All about cats.")
        assert (hit.headings, hit.page) == (("Habits",), 3)
        assert (hit.document_title, hit.source, hit.language) == ("Cats", "cats.txt", "en")

    async def test_several_collections_are_searched_together_and_only_those(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        first = await make_collection(store, tenant, owner, "First")
        second = await make_collection(store, tenant, owner, "Second")
        third = await make_collection(store, tenant, owner, "Third")
        found: list[ChunkId] = []
        for index, collection in enumerate((first, second, third)):
            document = await store.add_document(
                tenant, new_document(collection.id, f"doc{index}".encode())
            )
            ids = await indexed(store, tenant, collection, document, pieces("a shared word"))
            if collection is not third:
                found += ids

        hits = await store.search_text(tenant, [first.id, second.id], "shared", limit=10)

        assert sorted(h.chunk_id for h in hits) == sorted(found)

    async def test_the_limit_is_respected(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection = await make_collection(store, tenant, owner)
        document = await store.add_document(tenant, new_document(collection.id))
        await indexed(
            store, tenant, collection, document, pieces(*[f"router {n}" for n in range(6)])
        )

        assert len(await store.search_text(tenant, [collection.id], "router", limit=4)) == 4

    async def test_a_collection_of_another_tenant_is_not_there_to_search(
        self, store: KnowledgeStore, tenant: TenantId, owner: Owner
    ) -> None:
        collection, _ = await self.seeded(store, tenant, owner)

        with pytest.raises(CollectionNotFoundError):
            await store.search_text(TenantId(uuid4()), [collection.id], "router", limit=10)

    async def test_searching_no_collection_finds_nothing(
        self, store: KnowledgeStore, tenant: TenantId
    ) -> None:
        assert await store.search_text(tenant, [], "router", limit=10) == []
