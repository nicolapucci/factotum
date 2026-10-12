"""Searching the user's documents, through the knowledge service.

This service never reads a document itself: it asks the knowledge service for the passages that
answer a message, acting for the same owner the turn acts for (so what the owner may see, and only
that, comes back). What comes back is somebody else's words; the caller treats it so.
"""

from typing import Protocol

import httpx
from pydantic import ValidationError

from factotum.contracts.errors import KnowledgeUnavailableError
from factotum.contracts.identity import Owner
from factotum.contracts.knowledge import MAX_QUERY_CHARS, SearchRequest, SearchResult
from factotum.model_client import Delegation


class DocumentSearch(Protocol):
    async def search(
        self, owner: Owner, query: str, *, limit: int, min_similarity: float | None = None
    ) -> SearchResult:
        """The passages of ``owner``'s documents that answer ``query``, and only the ones at least
        ``min_similarity`` alike to it in meaning when that is given (ADR 0028). Raises
        ``KnowledgeUnavailableError`` when they cannot be searched."""
        ...


class KnowledgeClient:
    def __init__(self, client: httpx.AsyncClient, delegation: Delegation) -> None:
        self._client = client
        self._delegation = delegation

    async def search(
        self, owner: Owner, query: str, *, limit: int, min_similarity: float | None = None
    ) -> SearchResult:
        body = SearchRequest(
            query=query[:MAX_QUERY_CHARS], limit=limit, min_similarity=min_similarity
        ).model_dump(mode="json", exclude_none=True)
        try:
            response = await self._client.post(
                "/v1/search", json=body, headers=dict(self._delegation.headers(owner))
            )
        except httpx.HTTPError:
            raise KnowledgeUnavailableError("the knowledge service could not be reached") from None
        if response.status_code != 200:
            raise KnowledgeUnavailableError(
                f"the knowledge service answered with an error ({response.status_code})"
            )
        try:
            return SearchResult.model_validate(response.json())
        except (ValueError, ValidationError):
            raise KnowledgeUnavailableError(
                "the knowledge service answered with something that is not a search result"
            ) from None
