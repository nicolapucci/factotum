"""What a caller can do, and what they are told, against the service with everything in memory."""

from collections.abc import Callable

import pytest

from factotum.contracts.errors import (
    CollectionNotFoundError,
    ConflictError,
    DocumentNotFoundError,
    DocumentTooLargeError,
    EmbeddingNotConfiguredError,
    PolicyViolationError,
    UnsupportedDocumentError,
)
from factotum.contracts.identity import OwnerKind, Principal, Role
from factotum.contracts.knowledge import (
    Collection,
    CollectionOwner,
    CollectionUpdate,
    DocumentStatus,
    DocumentText,
)
from factotum.contracts.testing import make_tenant_principal, make_user_principal
from factotum.knowledge.document import Limits
from factotum.knowledge.service import KnowledgeService
from factotum.knowledge.store import JobKind
from factotum.knowledge.testing import Harness

ADMIN = frozenset({Role.ADMIN})
MEMBER = frozenset({Role.MEMBER})
TEXT = "To reset the router hold the button for ten seconds until the light blinks."


@pytest.fixture
def alice() -> Principal:
    return make_user_principal(roles=ADMIN)


@pytest.fixture
def harness() -> Harness:
    return Harness()


class TestCollections:
    async def test_a_collection_lives_in_the_space_of_its_owners_embedding_model(
        self, harness: Harness, alice: Principal
    ) -> None:
        harness.embedder.space_id, harness.embedder.dimensions = "emb-special", 16

        collection = await harness.collection(alice, "Manuals")

        assert (collection.space_id, collection.dimensions) == ("emb-special", 16)
        assert collection.owner.kind is OwnerKind.USER
        assert collection.owner.id == alice.acts_as.id
        [(owner, _)] = harness.embedder.calls
        assert owner == collection.owner

    async def test_an_administrator_creates_a_collection_for_the_tenant(
        self, harness: Harness, alice: Principal
    ) -> None:
        collection = await harness.collection(alice, "Household", owner=CollectionOwner.TENANT)

        assert collection.owner.kind is OwnerKind.TENANT
        assert collection.owner.id == alice.tenant_id

    async def test_a_member_may_not_create_one_for_the_tenant(self, harness: Harness) -> None:
        member = make_user_principal(roles=MEMBER)

        with pytest.raises(PolicyViolationError):
            await harness.collection(member, owner=CollectionOwner.TENANT)

    async def test_a_shared_device_creates_nothing_and_asks_no_model(
        self, harness: Harness
    ) -> None:
        speaker = make_tenant_principal()

        with pytest.raises(PolicyViolationError):
            await harness.collection(speaker)

        assert harness.embedder.calls == []

    async def test_an_owner_without_an_embedding_model_cannot_have_a_collection(
        self, harness: Harness, alice: Principal
    ) -> None:
        harness.embedder.failures = [EmbeddingNotConfiguredError("no binding")]

        with pytest.raises(EmbeddingNotConfiguredError):
            await harness.collection(alice)

        assert await harness.service.list_collections(alice) == []

    async def test_a_name_is_not_used_twice_by_one_owner(
        self, harness: Harness, alice: Principal
    ) -> None:
        await harness.collection(alice, "Manuals")

        with pytest.raises(ConflictError):
            await harness.collection(alice, "manuals")

    async def test_a_person_lists_and_gets_what_they_may_see(
        self, harness: Harness, alice: Principal
    ) -> None:
        mine = await harness.collection(alice, "Mine")
        shared = await harness.collection(alice, "Shared", owner=CollectionOwner.TENANT)
        bob = make_user_principal(tenant_id=alice.tenant_id, roles=MEMBER)
        hers = await harness.collection(bob, "Hers")

        assert {c.id for c in await harness.service.list_collections(alice)} == {mine.id, shared.id}
        assert {c.id for c in await harness.service.list_collections(bob)} == {shared.id, hers.id}
        assert (await harness.service.get_collection(bob, shared.id)).id == shared.id
        with pytest.raises(CollectionNotFoundError):
            await harness.service.get_collection(bob, mine.id)

    async def test_a_person_changes_their_own_collection(
        self, harness: Harness, alice: Principal
    ) -> None:
        mine = await harness.collection(alice, "Mine")

        renamed = await harness.service.update_collection(
            alice, mine.id, CollectionUpdate(name="Renamed")
        )
        await harness.service.delete_collection(alice, mine.id)

        assert renamed.name == "Renamed"
        with pytest.raises(CollectionNotFoundError):
            await harness.service.get_collection(alice, mine.id)

    async def test_what_a_person_may_not_see_they_may_not_touch_and_are_not_told_exists(
        self, harness: Harness, alice: Principal
    ) -> None:
        mine = await harness.collection(alice, "Mine")
        bob = make_user_principal(tenant_id=alice.tenant_id, roles=ADMIN)

        for act in (
            lambda: harness.service.update_collection(bob, mine.id, CollectionUpdate(name="x")),
            lambda: harness.service.delete_collection(bob, mine.id),
            lambda: harness.service.reindex_collection(bob, mine.id),
            lambda: harness.service.add_text(bob, mine.id, DocumentText(title="t", text="x")),
            lambda: harness.service.list_documents(bob, mine.id),
        ):
            with pytest.raises(CollectionNotFoundError):
                await act()

    async def test_the_collection_of_the_tenant_is_seen_by_all_and_changed_by_administrators(
        self, harness: Harness, alice: Principal
    ) -> None:
        shared = await harness.collection(alice, "Household", owner=CollectionOwner.TENANT)
        bob = make_user_principal(tenant_id=alice.tenant_id, roles=MEMBER)
        speaker = make_tenant_principal(tenant_id=alice.tenant_id)

        for who in (bob, speaker):
            with pytest.raises(PolicyViolationError, match="but not change"):
                await harness.service.update_collection(who, shared.id, CollectionUpdate(name="x"))
            with pytest.raises(PolicyViolationError):
                await harness.service.delete_collection(who, shared.id)
        assert (await harness.service.get_collection(speaker, shared.id)).name == "Household"

    async def test_a_reindex_is_queued_for_the_worker(
        self, harness: Harness, alice: Principal
    ) -> None:
        collection = await harness.collection(alice)

        await harness.service.reindex_collection(alice, collection.id)

        job = await harness.store.claim_job("worker", lease_seconds=60)
        assert job is not None
        assert (job.kind, job.collection_id) == (JobKind.REINDEX, collection.id)


class TestAddingDocuments:
    async def collection(self, harness: Harness, alice: Principal) -> Collection:
        return await harness.collection(alice)

    async def test_a_text_becomes_a_document_waiting_to_be_read(
        self, harness: Harness, alice: Principal
    ) -> None:
        collection = await self.collection(harness, alice)

        document = await harness.service.add_text(
            alice, collection.id, DocumentText(title="Router", text=TEXT, language="en")
        )

        assert document.title == "Router"
        assert document.media_type == "text/plain"
        assert (document.language, document.language_declared) == ("en", True)
        assert document.status is DocumentStatus.PENDING
        assert document.size_bytes == len(TEXT.encode())

    async def test_what_a_file_is_comes_from_what_is_in_it_and_not_from_its_name(
        self, harness: Harness, alice: Principal, pdf: Callable[..., bytes]
    ) -> None:
        collection = await self.collection(harness, alice)
        book = pdf({"Chapter 1": [TEXT * 3]})

        document = await harness.service.add_file(
            alice, collection.id, content=book, filename="notes.txt"
        )

        assert document.media_type == "application/pdf"

    async def test_a_file_name_is_only_a_name(self, harness: Harness, alice: Principal) -> None:
        collection = await self.collection(harness, alice)

        document = await harness.service.add_file(
            alice,
            collection.id,
            content=TEXT.encode(),
            filename="C:\\Users\\alice\\..\\..\\secrets/../notes\x00.txt",
        )

        assert document.source == "notes.txt"
        assert document.title == "notes"

    async def test_a_title_that_was_given_wins_over_the_name(
        self, harness: Harness, alice: Principal
    ) -> None:
        collection = await self.collection(harness, alice)

        document = await harness.service.add_file(
            alice, collection.id, content=TEXT.encode(), filename="a.txt", title="The manual"
        )

        assert document.title == "The manual"

    async def test_without_a_name_it_is_untitled(self, harness: Harness, alice: Principal) -> None:
        collection = await self.collection(harness, alice)

        document = await harness.service.add_file(alice, collection.id, content=TEXT.encode())

        assert (document.title, document.source) == ("Untitled", None)

    async def test_a_language_is_kept_as_a_two_letter_code(
        self, harness: Harness, alice: Principal
    ) -> None:
        collection = await self.collection(harness, alice)

        document = await harness.service.add_file(
            alice, collection.id, content=TEXT.encode(), language="en-US"
        )

        assert (document.language, document.language_declared) == ("en", True)

    async def test_something_that_is_not_a_language_is_refused(
        self, harness: Harness, alice: Principal
    ) -> None:
        collection = await self.collection(harness, alice)

        with pytest.raises(ValueError, match="language"):
            await harness.service.add_file(
                alice, collection.id, content=TEXT.encode(), language="klingon-tlh-1"
            )

    async def test_a_file_that_is_nothing_is_refused_before_it_is_kept(
        self, harness: Harness, alice: Principal
    ) -> None:
        collection = await self.collection(harness, alice)

        with pytest.raises(UnsupportedDocumentError, match="empty"):
            await harness.service.add_file(alice, collection.id, content=b"")
        with pytest.raises(UnsupportedDocumentError, match="not supported"):
            await harness.service.add_file(
                alice, collection.id, content=b"\x89PNG\r\n\x1a\n\x00\x00\x00"
            )
        assert await harness.service.list_documents(alice, collection.id) == []

    async def test_a_file_that_is_too_big_is_refused_before_it_is_read(
        self, alice: Principal
    ) -> None:
        harness = Harness()
        small = KnowledgeService(harness.store, harness.embedder, limits=Limits(max_bytes=100))
        collection = await harness.collection(alice)

        with pytest.raises(DocumentTooLargeError, match="larger than 0 MiB"):
            await small.add_file(alice, collection.id, content=b"x" * 101)
        with pytest.raises(DocumentTooLargeError):
            await small.add_text(alice, collection.id, DocumentText(title="t", text="x" * 101))

    async def test_the_same_content_twice_is_a_conflict(
        self, harness: Harness, alice: Principal
    ) -> None:
        collection = await self.collection(harness, alice)
        await harness.service.add_file(
            alice, collection.id, content=TEXT.encode(), filename="a.txt"
        )

        with pytest.raises(ConflictError):
            await harness.service.add_file(
                alice, collection.id, content=TEXT.encode(), filename="b.txt"
            )

    async def test_a_shared_device_cannot_add_anything(
        self, harness: Harness, alice: Principal
    ) -> None:
        shared = await harness.collection(alice, "Household", owner=CollectionOwner.TENANT)
        speaker = make_tenant_principal(tenant_id=alice.tenant_id)

        with pytest.raises(PolicyViolationError):
            await harness.service.add_text(speaker, shared.id, DocumentText(title="t", text=TEXT))
        with pytest.raises(PolicyViolationError):
            await harness.service.add_file(speaker, shared.id, content=TEXT.encode())


class TestDocuments:
    async def test_what_was_added_can_be_listed_read_and_removed(
        self, harness: Harness, alice: Principal
    ) -> None:
        collection = await harness.collection(alice)
        document = await harness.add_text(alice, collection, "Router", TEXT)
        await harness.process()

        listed = await harness.service.list_documents(alice, collection.id)
        read = await harness.service.get_document(alice, document.id)

        assert [d.id for d in listed] == [document.id]
        assert read.status is DocumentStatus.READY
        await harness.service.delete_document(alice, document.id)
        with pytest.raises(DocumentNotFoundError):
            await harness.service.get_document(alice, document.id)

    async def test_the_document_of_a_collection_that_may_not_be_seen_is_not_there(
        self, harness: Harness, alice: Principal
    ) -> None:
        collection = await harness.collection(alice)
        document = await harness.add_text(alice, collection, "Router", TEXT)
        bob = make_user_principal(tenant_id=alice.tenant_id, roles=ADMIN)

        for act in (
            lambda: harness.service.get_document(bob, document.id),
            lambda: harness.service.delete_document(bob, document.id),
            lambda: harness.service.retry_document(bob, document.id),
        ):
            with pytest.raises(DocumentNotFoundError):
                await act()

    async def test_the_document_of_a_collection_that_may_be_seen_and_not_changed_says_so(
        self, harness: Harness, alice: Principal
    ) -> None:
        shared = await harness.collection(alice, "Household", owner=CollectionOwner.TENANT)
        document = await harness.add_text(alice, shared, "Rules", TEXT)
        member = make_user_principal(tenant_id=alice.tenant_id, roles=MEMBER)

        assert (await harness.service.get_document(member, document.id)).id == document.id
        with pytest.raises(PolicyViolationError):
            await harness.service.delete_document(member, document.id)

    async def test_a_document_that_failed_can_be_tried_again(
        self, harness: Harness, alice: Principal
    ) -> None:
        collection = await harness.collection(alice)
        document = await harness.add_text(alice, collection, "Router", TEXT)
        harness.embedder.failures = [EmbeddingNotConfiguredError("no binding")]
        await harness.process()
        assert (await harness.service.get_document(alice, document.id)).status is (
            DocumentStatus.FAILED
        )

        again = await harness.service.retry_document(alice, document.id)
        await harness.process()

        assert again.status is DocumentStatus.PENDING
        assert (await harness.service.get_document(alice, document.id)).status is (
            DocumentStatus.READY
        )

    async def test_a_document_that_did_not_fail_is_not_tried_again(
        self, harness: Harness, alice: Principal
    ) -> None:
        collection = await harness.collection(alice)
        document = await harness.add_text(alice, collection, "Router", TEXT)

        with pytest.raises(ConflictError):
            await harness.service.retry_document(alice, document.id)
