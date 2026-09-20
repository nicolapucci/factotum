from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from factotum.contracts.identity import Owner, Subject
from factotum.contracts.ids import ChunkId, CollectionId, DocumentId
from factotum.contracts.knowledge import (
    Collection,
    CollectionCreate,
    CollectionOwner,
    CollectionStatus,
    CollectionUpdate,
    Document,
    DocumentStatus,
    DocumentText,
    Location,
    Passage,
    SearchRequest,
    SearchResult,
    SearchWarning,
    SearchWarningCode,
)
from factotum.contracts.testing import make_tenant_id

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def collection(**overrides: Any) -> Collection:
    tenant_id = make_tenant_id()
    fields: dict[str, Any] = {
        "id": CollectionId(uuid4()),
        "tenant_id": tenant_id,
        "owner": Owner.of_subject(Subject.tenant(tenant_id)),
        "name": "manuals",
        "space_id": "emb-0123456789abcdef",
        "dimensions": 768,
        "created_at": NOW,
        "updated_at": NOW,
    }
    return Collection(**(fields | overrides))


def document(**overrides: Any) -> Document:
    fields: dict[str, Any] = {
        "id": DocumentId(uuid4()),
        "collection_id": CollectionId(uuid4()),
        "title": "Router manual",
        "media_type": "application/pdf",
        "size_bytes": 1000,
        "content_sha256": "a" * 64,
        "status": DocumentStatus.READY,
        "created_at": NOW,
        "updated_at": NOW,
    }
    return Document(**(fields | overrides))


def passage(**overrides: Any) -> Passage:
    fields: dict[str, Any] = {
        "chunk_id": ChunkId(uuid4()),
        "collection_id": CollectionId(uuid4()),
        "document_id": DocumentId(uuid4()),
        "document_title": "Router manual",
        "text": "Hold the reset button for ten seconds.",
        "context": "Resetting. Hold the reset button for ten seconds. Wait for the light.",
        "score": 0.03,
        "matched_by": ("vector", "text"),
    }
    return Passage(**(fields | overrides))


class TestCollectionCreate:
    def test_a_collection_is_the_users_own_unless_it_is_said_to_be_the_tenants(self) -> None:
        assert CollectionCreate(name="notes").owner is CollectionOwner.USER
        tenants = CollectionCreate(name="notes", owner=CollectionOwner.TENANT)
        assert tenants.owner is CollectionOwner.TENANT

    @pytest.mark.parametrize("name", ["", "   ", "x" * 121])
    def test_the_name_must_be_a_reasonable_one(self, name: str) -> None:
        with pytest.raises(ValidationError):
            CollectionCreate(name=name)

    def test_the_name_is_trimmed(self) -> None:
        assert CollectionCreate(name="  notes  ").name == "notes"

    def test_an_owner_that_does_not_exist_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            CollectionCreate.model_validate({"name": "notes", "owner": "device"})

    @pytest.mark.parametrize("field", ["space_id", "tenant_id", "dimensions"])
    def test_a_request_cannot_choose_the_space_or_the_tenant(self, field: str) -> None:
        with pytest.raises(ValidationError):
            CollectionCreate.model_validate({"name": "notes", field: "x"})


class TestCollectionUpdate:
    def test_something_must_change(self) -> None:
        with pytest.raises(ValidationError, match="nothing to change"):
            CollectionUpdate()

    def test_either_field_is_enough(self) -> None:
        assert CollectionUpdate(name="new").description is None
        assert CollectionUpdate(description="").name is None


class TestCollection:
    def test_it_records_the_space_and_not_the_model(self) -> None:
        assert "model" not in Collection.model_fields
        assert "provider" not in Collection.model_fields
        assert collection().space_id.startswith("emb-")

    def test_it_starts_active_and_empty(self) -> None:
        fresh = collection()

        assert fresh.status is CollectionStatus.ACTIVE
        assert (fresh.document_count, fresh.chunk_count) == (0, 0)
        assert fresh.languages == ()

    def test_timestamps_must_carry_a_time_zone(self) -> None:
        with pytest.raises(ValidationError):
            collection(created_at=datetime(2026, 9, 20, 12, 0))  # noqa: DTZ001 - the point

    @pytest.mark.parametrize("languages", [("EN",), ("english",), ("e",), ("en-us",)])
    def test_languages_are_iso_codes_in_lower_case(self, languages: tuple[str, ...]) -> None:
        with pytest.raises(ValidationError):
            collection(languages=languages)

    def test_it_round_trips_through_json(self) -> None:
        original = collection(languages=("en", "it"), document_count=3, chunk_count=120)

        assert Collection.model_validate_json(original.model_dump_json()) == original


class TestDocumentText:
    def test_a_title_and_some_text_are_needed(self) -> None:
        with pytest.raises(ValidationError):
            DocumentText(title="", text="hello")
        with pytest.raises(ValidationError):
            DocumentText(title="t", text="")

    def test_blank_text_is_no_text(self) -> None:
        with pytest.raises(ValidationError):
            DocumentText(title="t", text=" \n\t ")

    def test_the_language_may_be_declared(self) -> None:
        assert DocumentText(title="t", text="ciao", language="it").language == "it"


class TestDocument:
    @pytest.mark.parametrize("bad", ["abc", "A" * 64, "g" * 64])
    def test_the_sha_is_a_sha256_in_hex(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            document(content_sha256=bad)

    @pytest.mark.parametrize(
        ("status", "total", "done", "expected"),
        [
            (DocumentStatus.READY, 10, 10, 1.0),
            (DocumentStatus.READY, 0, 0, 1.0),
            (DocumentStatus.PENDING, 0, 0, 0.0),
            (DocumentStatus.PROCESSING, 10, 4, 0.4),
            (DocumentStatus.PROCESSING, 10, 25, 1.0),
            (DocumentStatus.FAILED, 10, 5, 0.5),
        ],
    )
    def test_progress_is_how_much_has_been_embedded(
        self, status: DocumentStatus, total: int, done: int, expected: float
    ) -> None:
        shown = document(status=status, chunk_count=total, chunks_embedded=done)

        assert shown.progress == pytest.approx(expected)

    def test_a_failure_says_why(self) -> None:
        failed = document(
            status=DocumentStatus.FAILED,
            error_code="unsupported_document",
            error_message="no text in it",
        )

        assert failed.error_code == "unsupported_document"

    def test_the_language_can_be_detected_or_declared(self) -> None:
        assert document(language="en").language_declared is False
        assert document(language="it", language_declared=True).language_declared is True


class TestPassage:
    def test_a_passage_is_always_untrusted_and_cannot_say_otherwise(self) -> None:
        assert passage().untrusted is True
        with pytest.raises(ValidationError):
            passage(untrusted=False)

    def test_it_says_how_it_was_found(self) -> None:
        assert passage(matched_by=("text",)).matched_by == ("text",)
        with pytest.raises(ValidationError):
            passage(matched_by=())
        with pytest.raises(ValidationError):
            passage(matched_by=("magic",))

    def test_the_location_is_for_citations(self) -> None:
        located = passage(location=Location(page=12, headings=("Networking", "Routers")))

        assert located.location.page == 12
        assert located.location.headings == ("Networking", "Routers")

    def test_a_page_starts_at_one(self) -> None:
        with pytest.raises(ValidationError):
            Location(page=0)

    def test_it_round_trips_through_json(self) -> None:
        original = passage(language="en")

        assert Passage.model_validate_json(original.model_dump_json()) == original


class TestSearch:
    def test_a_search_looks_everywhere_the_caller_may_by_default(self) -> None:
        request = SearchRequest(query="reset the router")

        assert request.collection_ids is None
        assert request.limit == 8

    @pytest.mark.parametrize("query", ["", "   ", "x" * 2001])
    def test_the_query_must_be_reasonable(self, query: str) -> None:
        with pytest.raises(ValidationError):
            SearchRequest(query=query)

    @pytest.mark.parametrize("limit", [0, 51, -1])
    def test_the_limit_has_bounds(self, limit: int) -> None:
        with pytest.raises(ValidationError):
            SearchRequest(query="q", limit=limit)

    def test_an_explicit_list_of_collections_cannot_be_empty(self) -> None:
        with pytest.raises(ValidationError):
            SearchRequest(query="q", collection_ids=())

    def test_a_result_may_be_empty_and_may_carry_warnings(self) -> None:
        warning = SearchWarning(
            collection_id=CollectionId(uuid4()),
            code=SearchWarningCode.NEEDS_REINDEX,
            message="the embedding model changed",
        )

        result = SearchResult(warnings=(warning,))

        assert result.passages == ()
        assert result.warnings[0].code is SearchWarningCode.NEEDS_REINDEX
