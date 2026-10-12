"""The HTTP interface, through the real routes, against the service with everything in memory."""

from collections.abc import AsyncIterator, Mapping
from typing import Any
from uuid import uuid4

import httpx
import pytest

from factotum.contracts.errors import (
    AuthError,
    EmbeddingNotConfiguredError,
    GatewayUnavailableError,
)
from factotum.contracts.identity import Principal, Role
from factotum.contracts.testing import (
    make_tenant_principal,
    make_user_principal,
)
from factotum.knowledge.api import create_app
from factotum.knowledge.document import Limits
from factotum.knowledge.service import KnowledgeService
from factotum.knowledge.testing import Harness

ALICE = make_user_principal(roles=frozenset({Role.ADMIN}))
BOB = make_user_principal(tenant_id=ALICE.tenant_id, roles=frozenset({Role.MEMBER}))
SPEAKER = make_tenant_principal(tenant_id=ALICE.tenant_id)
STRANGER = make_user_principal(roles=frozenset({Role.ADMIN}))
PEOPLE = {"alice": ALICE, "bob": BOB, "speaker": SPEAKER, "stranger": STRANGER}

TEXT = "To reset the router hold the button for ten seconds until the light blinks."


class Who:
    """Which of the people above is asking, from a header. Nobody else is let in."""

    def authenticate(self, headers: Mapping[str, str]) -> Principal:
        try:
            return PEOPLE[headers["x-test-user"]]
        except KeyError:
            raise AuthError("who is asking?") from None


class Rig:
    def __init__(self, harness: Harness, client: httpx.AsyncClient) -> None:
        self.harness = harness
        self.client = client

    async def call(self, who: str, method: str, url: str, **options: Any) -> httpx.Response:
        headers = {"x-test-user": who, **options.pop("headers", {})}
        return await self.client.request(method, url, headers=headers, **options)

    async def make_collection(self, who: str = "alice", name: str = "Manuals", **body: Any) -> str:
        response = await self.call(who, "POST", "/v1/collections", json={"name": name, **body})
        assert response.status_code == 201, response.text
        identity: str = response.json()["id"]
        return identity

    async def add_text(self, collection: str, text: str = TEXT, title: str = "Router") -> str:
        response = await self.call(
            "alice",
            "POST",
            f"/v1/collections/{collection}/documents/text",
            json={"title": title, "text": text, "language": "en"},
        )
        assert response.status_code == 202, response.text
        identity: str = response.json()["id"]
        return identity


async def rig_for(harness: Harness, **options: Any) -> AsyncIterator[Rig]:
    limits = options.pop("limits", None)
    service = (
        harness.service
        if limits is None
        else KnowledgeService(harness.store, harness.embedder, limits=limits)
    )
    app = create_app(
        Who(),
        service=service,
        max_upload_bytes=limits.max_bytes if limits else Limits().max_bytes,
        **options,
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://knowledge") as client:
        yield Rig(harness, client)


@pytest.fixture
async def rig() -> AsyncIterator[Rig]:
    async for made in rig_for(Harness()):
        yield made


def code(response: httpx.Response) -> str:
    error: dict[str, Any] = response.json()["error"]
    return str(error["code"])


class TestTheServiceItself:
    async def test_it_says_it_is_alive(self, rig: Rig) -> None:
        response = await rig.client.get("/healthz")

        assert (response.status_code, response.json()) == (200, {"status": "ok"})

    async def test_it_says_it_is_ready_when_what_it_needs_answers(self) -> None:
        async def answering() -> bool:
            return True

        async for made in rig_for(Harness(), ready=answering):
            response = await made.client.get("/readyz")

        assert (response.status_code, response.json()) == (200, {"status": "ok"})

    async def test_it_says_it_is_not_ready_when_it_does_not(self) -> None:
        async def silent() -> bool:
            return False

        async for made in rig_for(Harness(), ready=silent):
            response = await made.client.get("/readyz")

        assert (response.status_code, response.json()) == (503, {"status": "unavailable"})

    async def test_health_needs_no_credentials_and_the_rest_does(self, rig: Rig) -> None:
        assert (await rig.client.get("/readyz")).status_code == 200
        for method, url in (
            ("GET", "/v1/collections"),
            ("POST", "/v1/search"),
            ("GET", f"/v1/documents/{uuid4()}"),
        ):
            response = await rig.client.request(method, url)
            assert (response.status_code, code(response)) == (401, "auth_error")

    async def test_the_documentation_is_served_only_when_asked_for(self) -> None:
        async for made in rig_for(Harness(), docs=True):
            with_docs = await made.client.get("/openapi.json")
        async for made in rig_for(Harness()):
            without = await made.client.get("/openapi.json")

        assert with_docs.status_code == 200
        assert "/v1/search" in with_docs.json()["paths"]
        assert without.status_code == 404


class TestCollections:
    async def test_a_collection_is_created_read_changed_and_deleted(self, rig: Rig) -> None:
        created = await rig.call(
            "alice", "POST", "/v1/collections", json={"name": "Manuals", "description": "Devices"}
        )
        identity = created.json()["id"]

        assert created.status_code == 201
        assert created.json()["name"] == "Manuals"
        assert created.json()["status"] == "active"
        assert created.json()["owner"]["kind"] == "user"
        assert (
            await rig.call("alice", "GET", f"/v1/collections/{identity}")
        ).json() == created.json()

        renamed = await rig.call(
            "alice", "PATCH", f"/v1/collections/{identity}", json={"name": "Guides"}
        )
        assert renamed.json()["name"] == "Guides"
        assert renamed.json()["description"] == "Devices"

        gone = await rig.call("alice", "DELETE", f"/v1/collections/{identity}")
        assert (gone.status_code, gone.content) == (204, b"")
        missing = await rig.call("alice", "GET", f"/v1/collections/{identity}")
        assert (missing.status_code, code(missing)) == (404, "collection_not_found")

    async def test_the_list_holds_what_the_caller_may_see(self, rig: Rig) -> None:
        mine = await rig.make_collection("alice", "Mine")
        shared = await rig.make_collection("alice", "Household", owner="tenant")

        alice = await rig.call("alice", "GET", "/v1/collections")
        bob = await rig.call("bob", "GET", "/v1/collections")
        speaker = await rig.call("speaker", "GET", "/v1/collections")
        stranger = await rig.call("stranger", "GET", "/v1/collections")

        def ids(response: httpx.Response) -> set[str]:
            return {c["id"] for c in response.json()["collections"]}

        assert ids(alice) == {mine, shared}
        assert ids(bob) == {shared}
        assert ids(speaker) == {shared}
        assert ids(stranger) == set()

    async def test_what_a_person_may_not_see_is_not_there_and_says_nothing_else(
        self, rig: Rig
    ) -> None:
        mine = await rig.make_collection("alice")

        for method, url, options in (
            ("GET", f"/v1/collections/{mine}", {}),
            ("PATCH", f"/v1/collections/{mine}", {"json": {"name": "x"}}),
            ("DELETE", f"/v1/collections/{mine}", {}),
            ("POST", f"/v1/collections/{mine}/reindex", {}),
            ("GET", f"/v1/collections/{mine}/documents", {}),
        ):
            response = await rig.call("bob", method, url, **options)
            assert (response.status_code, code(response)) == (404, "collection_not_found")

    async def test_the_collection_of_the_tenant_may_be_seen_and_not_changed_by_a_member(
        self, rig: Rig
    ) -> None:
        shared = await rig.make_collection("alice", "Household", owner="tenant")

        read = await rig.call("bob", "GET", f"/v1/collections/{shared}")
        changed = await rig.call("bob", "PATCH", f"/v1/collections/{shared}", json={"name": "x"})

        assert read.status_code == 200
        assert (changed.status_code, code(changed)) == (403, "policy_violation")

    async def test_a_member_may_not_create_a_collection_for_the_tenant(self, rig: Rig) -> None:
        response = await rig.call(
            "bob", "POST", "/v1/collections", json={"name": "Household", "owner": "tenant"}
        )

        assert (response.status_code, code(response)) == (403, "policy_violation")

    async def test_a_shared_device_cannot_create_anything(self, rig: Rig) -> None:
        response = await rig.call("speaker", "POST", "/v1/collections", json={"name": "Mine"})

        assert (response.status_code, code(response)) == (403, "policy_violation")

    async def test_a_name_is_not_used_twice(self, rig: Rig) -> None:
        await rig.make_collection(name="Manuals")

        response = await rig.call("alice", "POST", "/v1/collections", json={"name": "manuals"})

        assert (response.status_code, code(response)) == (409, "conflict")

    async def test_an_owner_without_an_embedding_model_is_told_so(self) -> None:
        harness = Harness()
        harness.embedder.failures = [EmbeddingNotConfiguredError("no binding")]

        async for made in rig_for(harness):
            response = await made.call("alice", "POST", "/v1/collections", json={"name": "Mine"})

        assert (response.status_code, code(response)) == (409, "embedding_not_configured")

    async def test_a_gateway_that_is_down_is_a_503(self) -> None:
        harness = Harness()
        harness.embedder.failures = [GatewayUnavailableError("down")]

        async for made in rig_for(harness):
            response = await made.call("alice", "POST", "/v1/collections", json={"name": "Mine"})

        assert (response.status_code, code(response)) == (503, "gateway_unavailable")

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"name": ""},
            {"name": "ok", "owner": "device"},
            {"name": "ok", "space_id": "chosen"},
            {"name": "ok", "tenant_id": "someone-else"},
        ],
    )
    async def test_a_request_that_breaks_the_contract_is_a_422_that_echoes_nothing(
        self, rig: Rig, body: dict[str, Any]
    ) -> None:
        response = await rig.call("alice", "POST", "/v1/collections", json=body)

        assert (response.status_code, code(response)) == (422, "invalid_request_body")
        assert "chosen" not in response.text
        assert "someone-else" not in response.text

    async def test_an_id_that_is_not_an_id_is_a_422(self, rig: Rig) -> None:
        response = await rig.call("alice", "GET", "/v1/collections/not-an-id")

        assert (response.status_code, code(response)) == (422, "invalid_request_body")

    async def test_a_change_that_changes_nothing_is_a_422(self, rig: Rig) -> None:
        mine = await rig.make_collection()

        response = await rig.call("alice", "PATCH", f"/v1/collections/{mine}", json={})

        assert response.status_code == 422

    async def test_a_reindex_is_accepted_and_left_to_the_workers(self, rig: Rig) -> None:
        mine = await rig.make_collection()
        await rig.add_text(mine)
        await rig.harness.process()
        rig.harness.embedder.space_id = "emb-another-model"

        accepted = await rig.call("alice", "POST", f"/v1/collections/{mine}/reindex")
        await rig.harness.process()

        assert (accepted.status_code, accepted.content) == (202, b"")
        result = await rig.call("alice", "GET", f"/v1/collections/{mine}")
        assert result.json()["space_id"] == "emb-another-model"


class TestDocuments:
    async def test_a_file_is_accepted_at_once_and_read_afterwards(self, rig: Rig) -> None:
        mine = await rig.make_collection()

        accepted = await rig.call(
            "alice",
            "POST",
            f"/v1/collections/{mine}/documents",
            files={"file": ("router notes.txt", TEXT.encode(), "text/plain")},
            data={"title": "Router", "language": "en-US"},
        )

        assert accepted.status_code == 202
        document = accepted.json()
        assert (document["title"], document["source"]) == ("Router", "router notes.txt")
        assert (document["status"], document["language"]) == ("pending", "en")
        assert "data" not in document

        await rig.harness.process()
        done = await rig.call("alice", "GET", f"/v1/documents/{document['id']}")
        assert done.json()["status"] == "ready"
        assert done.json()["chunks_embedded"] == done.json()["chunk_count"] > 0

    async def test_a_title_left_empty_is_the_name_of_the_file(self, rig: Rig) -> None:
        mine = await rig.make_collection()

        accepted = await rig.call(
            "alice",
            "POST",
            f"/v1/collections/{mine}/documents",
            files={"file": ("router.txt", TEXT.encode(), "text/plain")},
            data={"title": "", "language": ""},
        )

        assert (accepted.json()["title"], accepted.json()["language"]) == ("router", None)

    async def test_a_text_is_accepted_as_it_is(self, rig: Rig) -> None:
        mine = await rig.make_collection()

        accepted = await rig.call(
            "alice",
            "POST",
            f"/v1/collections/{mine}/documents/text",
            json={"title": "Router", "text": TEXT},
        )

        assert accepted.status_code == 202
        assert accepted.json()["media_type"] == "text/plain"

    async def test_what_a_file_is_comes_from_what_is_in_it(self, rig: Rig) -> None:
        mine = await rig.make_collection()

        accepted = await rig.call(
            "alice",
            "POST",
            f"/v1/collections/{mine}/documents",
            files={"file": ("photo.txt", b"\x89PNG\r\n\x1a\n\x00\x00\x00", "text/plain")},
        )

        assert (accepted.status_code, code(accepted)) == (415, "unsupported_document")

    async def test_an_empty_file_is_refused(self, rig: Rig) -> None:
        mine = await rig.make_collection()

        response = await rig.call(
            "alice",
            "POST",
            f"/v1/collections/{mine}/documents",
            files={"file": ("empty.txt", b"", "text/plain")},
        )

        assert (response.status_code, code(response)) == (415, "unsupported_document")

    async def test_the_same_content_twice_is_a_conflict(self, rig: Rig) -> None:
        mine = await rig.make_collection()
        upload = {"file": ("a.txt", TEXT.encode(), "text/plain")}
        await rig.call("alice", "POST", f"/v1/collections/{mine}/documents", files=upload)

        again = await rig.call(
            "alice",
            "POST",
            f"/v1/collections/{mine}/documents",
            files={"file": ("b.txt", TEXT.encode(), "text/plain")},
        )

        assert (again.status_code, code(again)) == (409, "conflict")

    async def test_a_file_over_the_limit_is_refused_and_never_kept(self) -> None:
        harness = Harness()
        async for made in rig_for(harness, limits=Limits(max_bytes=100)):
            mine = await made.make_collection()

            response = await made.call(
                "alice",
                "POST",
                f"/v1/collections/{mine}/documents",
                files={"file": ("big.txt", b"x" * 101, "text/plain")},
            )
            listed = await made.call("alice", "GET", f"/v1/collections/{mine}/documents")

        assert (response.status_code, code(response)) == (413, "document_too_large")
        assert listed.json() == {"documents": []}

    async def test_a_body_far_over_the_limit_is_refused_before_it_is_read(self) -> None:
        async for made in rig_for(Harness(), limits=Limits(max_bytes=100)):
            mine = await made.make_collection()

            response = await made.call(
                "alice",
                "POST",
                f"/v1/collections/{mine}/documents",
                files={"file": ("huge.txt", b"x" * (2 * 1024 * 1024), "text/plain")},
            )

        assert (response.status_code, code(response)) == (413, "payload_too_large")

    @pytest.mark.parametrize("language", ["klingon-tlh-1!", "e", "english"])
    async def test_a_language_that_is_not_a_code_is_a_422(self, rig: Rig, language: str) -> None:
        mine = await rig.make_collection()

        response = await rig.call(
            "alice",
            "POST",
            f"/v1/collections/{mine}/documents",
            files={"file": ("a.txt", TEXT.encode(), "text/plain")},
            data={"language": language},
        )

        assert response.status_code == 422

    async def test_an_upload_without_a_file_is_a_422(self, rig: Rig) -> None:
        mine = await rig.make_collection()

        response = await rig.call(
            "alice", "POST", f"/v1/collections/{mine}/documents", data={"title": "x"}
        )

        assert response.status_code == 422

    async def test_a_shared_device_cannot_add_a_document(self, rig: Rig) -> None:
        shared = await rig.make_collection("alice", "Household", owner="tenant")

        response = await rig.call(
            "speaker",
            "POST",
            f"/v1/collections/{shared}/documents/text",
            json={"title": "x", "text": TEXT},
        )

        assert (response.status_code, code(response)) == (403, "policy_violation")

    async def test_the_documents_of_a_collection_are_listed_in_pages(self, rig: Rig) -> None:
        mine = await rig.make_collection()
        added = [await rig.add_text(mine, f"{TEXT} {n}", f"Doc {n}") for n in range(3)]

        everything = await rig.call("alice", "GET", f"/v1/collections/{mine}/documents")
        page = await rig.call(
            "alice", "GET", f"/v1/collections/{mine}/documents", params={"limit": 2, "offset": 1}
        )

        assert [d["id"] for d in everything.json()["documents"]] == list(reversed(added))
        assert [d["id"] for d in page.json()["documents"]] == [added[1], added[0]]

    @pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 501}, {"offset": -1}])
    async def test_a_page_that_makes_no_sense_is_a_422(
        self, rig: Rig, params: dict[str, int]
    ) -> None:
        mine = await rig.make_collection()

        response = await rig.call(
            "alice", "GET", f"/v1/collections/{mine}/documents", params=params
        )

        assert response.status_code == 422

    async def test_a_document_is_read_and_removed(self, rig: Rig) -> None:
        mine = await rig.make_collection()
        document = await rig.add_text(mine)

        read = await rig.call("alice", "GET", f"/v1/documents/{document}")
        gone = await rig.call("alice", "DELETE", f"/v1/documents/{document}")
        after = await rig.call("alice", "GET", f"/v1/documents/{document}")

        assert read.status_code == 200
        assert (gone.status_code, gone.content) == (204, b"")
        assert (after.status_code, code(after)) == (404, "document_not_found")

    async def test_the_document_of_a_collection_that_may_not_be_seen_is_not_there(
        self, rig: Rig
    ) -> None:
        mine = await rig.make_collection()
        document = await rig.add_text(mine)

        for method, url in (
            ("GET", f"/v1/documents/{document}"),
            ("DELETE", f"/v1/documents/{document}"),
            ("POST", f"/v1/documents/{document}/retry"),
        ):
            response = await rig.call("bob", method, url)
            assert (response.status_code, code(response)) == (404, "document_not_found")

    async def test_a_document_that_failed_can_be_tried_again(self) -> None:
        harness = Harness()
        harness.embedder.failures = [None, EmbeddingNotConfiguredError("no binding")]
        async for made in rig_for(harness):
            mine = await made.make_collection()
            document = await made.add_text(mine)
            await harness.process()
            failed = await made.call("alice", "GET", f"/v1/documents/{document}")

            retried = await made.call("alice", "POST", f"/v1/documents/{document}/retry")
            await harness.process()
            done = await made.call("alice", "GET", f"/v1/documents/{document}")

        assert failed.json()["status"] == "failed"
        assert failed.json()["error_code"] == "embedding_not_configured"
        assert retried.status_code == 202
        assert retried.json()["status"] == "pending"
        assert done.json()["status"] == "ready"

    async def test_a_document_that_did_not_fail_is_not_tried_again(self, rig: Rig) -> None:
        mine = await rig.make_collection()
        document = await rig.add_text(mine)

        response = await rig.call("alice", "POST", f"/v1/documents/{document}/retry")

        assert (response.status_code, code(response)) == (409, "conflict")


class TestSearching:
    async def test_a_search_answers_with_passages_that_say_they_are_somebody_elses_words(
        self, rig: Rig
    ) -> None:
        mine = await rig.make_collection()
        await rig.add_text(mine)
        await rig.harness.process()

        response = await rig.call("alice", "POST", "/v1/search", json={"query": "reset the router"})

        assert response.status_code == 200
        [passage, *_] = response.json()["passages"]
        assert passage["text"] == TEXT
        assert passage["untrusted"] is True
        assert passage["matched_by"] == ["vector", "text"]
        assert passage["document_title"] == "Router"
        assert passage["collection_id"] == mine
        assert 0 < passage["score"] <= 1
        assert response.json()["warnings"] == []

    async def test_a_search_can_be_limited_to_some_collections_and_some_passages(
        self, rig: Rig
    ) -> None:
        first = await rig.make_collection(name="First")
        second = await rig.make_collection(name="Second")
        await rig.add_text(first, TEXT + " First.", "One")
        await rig.add_text(second, TEXT + " Second.", "Two")
        await rig.harness.process()

        response = await rig.call(
            "alice",
            "POST",
            "/v1/search",
            json={"query": "router", "collection_ids": [second], "limit": 1},
        )

        assert [p["collection_id"] for p in response.json()["passages"]] == [second]

    async def test_a_search_does_not_reach_what_is_another_persons(self, rig: Rig) -> None:
        mine = await rig.make_collection()
        await rig.add_text(mine)
        await rig.harness.process()

        bob = await rig.call("bob", "POST", "/v1/search", json={"query": "router"})
        stranger = await rig.call("stranger", "POST", "/v1/search", json={"query": "router"})

        assert bob.json() == {"passages": [], "warnings": [], "left_out": 0}
        assert stranger.json() == {"passages": [], "warnings": [], "left_out": 0}

    async def test_a_search_can_leave_out_what_is_not_alike_enough_in_meaning(
        self, rig: Rig
    ) -> None:
        mine = await rig.make_collection()
        await rig.add_text(mine)
        await rig.harness.process()

        about = await rig.call(
            "alice",
            "POST",
            "/v1/search",
            json={"query": "reset the router", "min_similarity": 0.4},
        )
        about_nothing = await rig.call(
            "alice", "POST", "/v1/search", json={"query": "submarine", "min_similarity": 0.4}
        )

        [passage] = about.json()["passages"]
        assert passage["similarity"] > 0.4
        assert about.json()["left_out"] == 0
        assert (about_nothing.json()["passages"], about_nothing.json()["left_out"]) == ([], 1)

    async def test_a_floor_that_is_not_one_is_not_accepted(self, rig: Rig) -> None:
        response = await rig.call(
            "alice", "POST", "/v1/search", json={"query": "router", "min_similarity": 2}
        )

        assert response.status_code == 422

    async def test_asking_for_a_collection_that_may_not_be_seen_is_asking_for_one_that_is_not_there(
        self, rig: Rig
    ) -> None:
        mine = await rig.make_collection()

        response = await rig.call(
            "bob", "POST", "/v1/search", json={"query": "router", "collection_ids": [mine]}
        )

        assert (response.status_code, code(response)) == (404, "collection_not_found")

    async def test_a_shared_device_finds_what_is_the_tenants(self, rig: Rig) -> None:
        shared = await rig.make_collection("alice", "Household", owner="tenant")
        await rig.add_text(shared)
        await rig.harness.process()

        response = await rig.call("speaker", "POST", "/v1/search", json={"query": "reset router"})

        assert [p["collection_id"] for p in response.json()["passages"]] == [shared]

    async def test_a_search_that_could_not_use_meaning_says_why(self, rig: Rig) -> None:
        mine = await rig.make_collection()
        await rig.add_text(mine)
        await rig.harness.process()
        rig.harness.embedder.failures = [GatewayUnavailableError("down")]

        response = await rig.call("alice", "POST", "/v1/search", json={"query": "reset button"})

        assert response.status_code == 200
        assert response.json()["passages"][0]["matched_by"] == ["text"]
        [warning] = response.json()["warnings"]
        assert (warning["collection_id"], warning["code"]) == (mine, "vector_unavailable")

    @pytest.mark.parametrize(
        "body",
        [{}, {"query": ""}, {"query": "x", "limit": 0}, {"query": "x", "collection_ids": []}],
    )
    async def test_a_question_that_makes_no_sense_is_a_422(
        self, rig: Rig, body: dict[str, Any]
    ) -> None:
        response = await rig.call("alice", "POST", "/v1/search", json=body)

        assert (response.status_code, code(response)) == (422, "invalid_request_body")

    async def test_what_was_asked_is_never_echoed_in_an_error(self, rig: Rig) -> None:
        secret = "sk-live-0123456789abcdefghijklmnop"

        response = await rig.call("alice", "POST", "/v1/search", json={"query": secret, "limit": 0})

        assert response.status_code == 422
        assert secret not in response.text
