"""The normalized embeddings API."""

import hashlib
from typing import Self

from pydantic import Field, field_validator, model_validator

from factotum.contracts.base import Contract
from factotum.contracts.chat import Usage
from factotum.contracts.models import ProviderKind

MAX_INPUTS_PER_REQUEST = 256


class EmbeddingSpace(Contract):
    """The space vectors live in: vectors from different spaces must never be compared.

    A collection records the id of the space its vectors were made in, so a change of embedding
    model is noticed and the collection re-indexed, instead of mixing spaces silently.
    """

    provider: ProviderKind
    model: str = Field(min_length=1, max_length=200)
    dimensions: int = Field(ge=1)

    @property
    def id(self) -> str:
        """An opaque, stable identifier of (provider, model, dimensions)."""
        digest = hashlib.sha256(f"{self.provider.value}|{self.model}|{self.dimensions}".encode())
        return f"emb-{digest.hexdigest()[:16]}"


class EmbeddingRequest(Contract):
    inputs: tuple[str, ...] = Field(min_length=1, max_length=MAX_INPUTS_PER_REQUEST)

    @field_validator("inputs")
    @classmethod
    def _inputs_are_not_blank(cls, inputs: tuple[str, ...]) -> tuple[str, ...]:
        if any(not text.strip() for text in inputs):
            raise ValueError("inputs cannot be blank")
        return inputs


class EmbeddingResult(Contract):
    vectors: tuple[tuple[float, ...], ...] = Field(min_length=1)
    """One vector per input, in the same order."""

    dimensions: int = Field(ge=1)
    space_id: str
    usage: Usage | None = None

    @model_validator(mode="after")
    def _vectors_have_the_declared_size(self) -> Self:
        if any(len(vector) != self.dimensions for vector in self.vectors):
            raise ValueError("every vector must have the declared number of dimensions")
        return self
