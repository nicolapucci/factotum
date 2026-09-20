import json
from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
import respx

from factotum.common.config import Environment, ServiceSettings
from factotum.contracts.errors import KnowledgeUnavailableError
from factotum.contracts.identity import Owner, OwnerKind
from factotum.contracts.knowledge import MAX_QUERY_CHARS, SearchResult
from factotum.conversation.documents import KnowledgeClient
from factotum.conversation.testing import passage
from factotum.model_client import DevDelegation

URL = "http://knowledge.test"
USER = Owner(kind=OwnerKind.USER, id=uuid4())
TENANT = Owner(kind=OwnerKind.TENANT, id=uuid4())


@pytest.fixture
async def documents() -> AsyncIterator[KnowledgeClient]:
    settings = ServiceSettings(environment=Environment.TEST, _env_file=None)
    async with httpx.AsyncClient(base_url=URL) as client:
        yield KnowledgeClient(client, DevDelegation(settings))


class TestSearching:
    @respx.mock
    async def test_the_passages_come_back_and_the_search_is_for_the_owner(
        self, documents: KnowledgeClient
    ) -> None:
        found = SearchResult(passages=(passage("Hold the button."),))
        route = respx.post(f"{URL}/v1/search").respond(200, json=found.model_dump(mode="json"))

        result = await documents.search(TENANT, "how do I reset the router?", limit=6)

        assert [p.text for p in result.passages] == ["Hold the button."]
        sent = route.calls.last.request
        assert sent.headers["x-factotum-dev-subject"] == "tenant"
        assert json.loads(sent.content) == {"query": "how do I reset the router?", "limit": 6}

    @respx.mock
    async def test_the_warnings_of_the_search_come_back_too(
        self, documents: KnowledgeClient
    ) -> None:
        warning = {"collection_id": str(uuid4()), "code": "needs_reindex", "message": "words only"}
        respx.post(f"{URL}/v1/search").respond(200, json={"passages": [], "warnings": [warning]})

        result = await documents.search(USER, "router", limit=3)

        assert result.warnings[0].message == "words only"

    @respx.mock
    async def test_a_message_longer_than_a_search_can_be_is_cut_to_it(
        self, documents: KnowledgeClient
    ) -> None:
        route = respx.post(f"{URL}/v1/search").respond(200, json={"passages": []})

        await documents.search(USER, "word " * 1000, limit=3)

        assert len(json.loads(route.calls.last.request.content)["query"]) <= MAX_QUERY_CHARS


class TestWhenItCannotBeSearched:
    @respx.mock
    async def test_a_service_that_cannot_be_reached_is_unavailable(
        self, documents: KnowledgeClient
    ) -> None:
        respx.post(f"{URL}/v1/search").mock(side_effect=httpx.ConnectError("refused"))

        with pytest.raises(KnowledgeUnavailableError, match="could not be reached"):
            await documents.search(USER, "router", limit=3)

    @pytest.mark.parametrize("status", [401, 403, 404, 422, 500, 503])
    @respx.mock
    async def test_an_error_is_unavailable_whatever_it_was(
        self, documents: KnowledgeClient, status: int
    ) -> None:
        respx.post(f"{URL}/v1/search").respond(status, json={"error": {"code": "x"}})

        with pytest.raises(KnowledgeUnavailableError, match=str(status)):
            await documents.search(USER, "router", limit=3)

    @respx.mock
    async def test_an_answer_that_is_not_a_search_result_is_unavailable(
        self, documents: KnowledgeClient
    ) -> None:
        respx.post(f"{URL}/v1/search").respond(200, json={"passages": "none"})

        with pytest.raises(KnowledgeUnavailableError, match="not a search result"):
            await documents.search(USER, "router", limit=3)

    @respx.mock
    async def test_an_answer_that_is_not_json_is_unavailable(
        self, documents: KnowledgeClient
    ) -> None:
        respx.post(f"{URL}/v1/search").respond(200, content=b"<html>an outage</html>")

        with pytest.raises(KnowledgeUnavailableError, match="not a search result"):
            await documents.search(USER, "router", limit=3)
