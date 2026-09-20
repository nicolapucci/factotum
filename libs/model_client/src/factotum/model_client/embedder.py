"""Turning text into vectors, through the model gateway.

A service never knows which model, provider or key: it asks the gateway for the embeddings of
*an owner* and gets vectors back together with the id of the space they live in and their size.
That id is what a collection (or a log of exchanges) remembers, so a change of model behind the
owner's role is noticed (the ids differ) instead of two spaces being compared.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

import httpx
from pydantic import ValidationError

from factotum.contracts.embeddings import MAX_INPUTS_PER_REQUEST, EmbeddingRequest, EmbeddingResult
from factotum.contracts.errors import GatewayError, GatewayUnavailableError
from factotum.contracts.identity import Owner
from factotum.contracts.models import ModelRole
from factotum.model_client.gateway import Delegation, error_from

DEFAULT_BATCH = 64

PROBE = "a text to learn where the vectors of the embedding model live"
"""What is embedded to learn the space and the size of an owner's vectors."""


@dataclass(frozen=True, slots=True)
class Embedded:
    """The vectors of some texts, in order, and where they live."""

    vectors: tuple[tuple[float, ...], ...]
    space_id: str
    dimensions: int


class Embedder(Protocol):
    async def embed(self, owner: Owner, texts: Sequence[str]) -> Embedded:
        """Embed ``texts`` with the embedding model of ``owner``. Raises
        ``EmbeddingNotConfiguredError`` when the owner has none, ``ContextTooLongError`` when a
        text does not fit the model, and a ``GatewayError`` (``GatewayUnavailableError`` for one
        that asking again may cure) when the gateway cannot answer."""
        ...


class GatewayEmbedder:
    def __init__(
        self, client: httpx.AsyncClient, delegation: Delegation, *, batch: int = DEFAULT_BATCH
    ) -> None:
        self._client = client
        self._delegation = delegation
        self._batch = max(1, min(batch, MAX_INPUTS_PER_REQUEST))

    async def embed(self, owner: Owner, texts: Sequence[str]) -> Embedded:
        if not texts:
            raise ValueError("there is nothing to embed")
        headers = dict(self._delegation.headers(owner))
        vectors: list[tuple[float, ...]] = []
        space_id = ""
        dimensions = 0
        for start in range(0, len(texts), self._batch):
            part = texts[start : start + self._batch]
            result = await self._request(headers, part)
            if vectors and (result.space_id, result.dimensions) != (space_id, dimensions):
                raise GatewayError("the embedding model changed while the texts were embedded")
            space_id, dimensions = result.space_id, result.dimensions
            vectors.extend(result.vectors)
        return Embedded(tuple(vectors), space_id, dimensions)

    async def _request(self, headers: Mapping[str, str], texts: Sequence[str]) -> EmbeddingResult:
        body = EmbeddingRequest(inputs=tuple(texts)).model_dump(mode="json")
        try:
            response = await self._client.post("/v1/embeddings", json=body, headers=headers)
        except httpx.HTTPError:
            raise GatewayUnavailableError("the model gateway could not be reached") from None
        if response.status_code != 200:
            raise error_from(response, ModelRole.EMBEDDING)
        try:
            result = EmbeddingResult.model_validate(response.json())
        except (ValueError, ValidationError):
            raise GatewayError(
                "the model gateway answered with something that is not an embedding"
            ) from None
        if len(result.vectors) != len(texts):
            raise GatewayError("the model gateway answered with a vector too few or too many")
        return result
