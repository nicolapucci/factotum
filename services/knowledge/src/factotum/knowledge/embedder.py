"""Turning text into vectors, through the model gateway.

This service never knows which model, provider or key: it asks the gateway for the embeddings of
*a collection's owner* and gets vectors back together with the id of the space they live in and
their size. That id is what a collection remembers, so a change of model behind the owner's role is
noticed (the ids differ) and the collection re-indexed, instead of two spaces being compared.

Acting for the owner is delegation: the gateway holds the owner's bindings. Until the hub issues
service credentials (Slice 7) the only delegation is the development one, which the gateway's own
development authentication understands.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

import httpx
from pydantic import ValidationError

from factotum.common.config import Environment, ServiceSettings
from factotum.common.devauth import SUBJECT_HEADER
from factotum.contracts.embeddings import MAX_INPUTS_PER_REQUEST, EmbeddingRequest, EmbeddingResult
from factotum.contracts.errors import (
    AuthError,
    BindingNotFoundError,
    EmbeddingNotConfiguredError,
    FactotumError,
    GatewayError,
    GatewayUnavailableError,
    RateLimitedError,
)
from factotum.contracts.identity import Owner, OwnerKind

DEFAULT_BATCH = 64


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


class Delegation(Protocol):
    def headers(self, owner: Owner) -> Mapping[str, str]:
        """What proves to the gateway that this service acts for ``owner``."""
        ...


class DevDelegation:
    """The development delegation: the gateway's development authentication takes the subject from
    a header. It refuses to exist outside development and tests."""

    def __init__(self, settings: ServiceSettings) -> None:
        if settings.environment not in {Environment.DEVELOPMENT, Environment.TEST}:
            raise AuthError(
                "the development delegation is disabled outside the development and test "
                f"environments (environment is {settings.environment.value!r})"
            )

    def headers(self, owner: Owner) -> Mapping[str, str]:
        match owner.kind:
            case OwnerKind.USER:
                return {SUBJECT_HEADER: "user"}
            case OwnerKind.TENANT:
                return {SUBJECT_HEADER: "tenant"}
            case OwnerKind.DEVICE:
                raise ValueError("a collection is owned by a user or by the tenant")


def _error_classes() -> dict[str, type[FactotumError]]:
    found: dict[str, type[FactotumError]] = {}
    pending: list[type[FactotumError]] = [FactotumError]
    while pending:
        kind = pending.pop()
        found.setdefault(kind.code, kind)
        pending.extend(kind.__subclasses__())
    return found


def error_from(response: httpx.Response) -> FactotumError:
    """The typed error the gateway answered with. The kind is in the ``code``; a body that has none
    (a proxy's page, an outage) is judged by its status."""
    code = message = None
    details: Mapping[str, object] = {}
    try:
        error = response.json()["error"]
        code, message = str(error["code"]), str(error["message"])
        details = error.get("details") or {}
    except (ValueError, KeyError, TypeError):
        pass

    unavailable = response.status_code in {502, 503, 504} and code is None
    if unavailable:
        return GatewayUnavailableError("the model gateway is not answering")
    if code is None or message is None:
        return GatewayError(f"the model gateway answered with an error ({response.status_code})")

    if code == RateLimitedError.code:
        retry_after = response.headers.get("Retry-After", "")
        return RateLimitedError(
            message, retry_after_s=float(retry_after) if retry_after.isdigit() else None
        )
    if code == BindingNotFoundError.code:
        return EmbeddingNotConfiguredError("no embedding model is set up for the owner")
    kind = _error_classes().get(code, GatewayError)
    return kind(message, details=details)


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
            raise error_from(response)
        try:
            result = EmbeddingResult.model_validate(response.json())
        except (ValueError, ValidationError):
            raise GatewayError(
                "the model gateway answered with something that is not an embedding"
            ) from None
        if len(result.vectors) != len(texts):
            raise GatewayError("the model gateway answered with a vector too few or too many")
        return result
