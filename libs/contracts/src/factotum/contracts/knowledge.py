"""Collections of documents, and what searching them answers.

A *collection* belongs to a user or to the tenant (the owner decides who sees it) and lives in one
embedding space: vectors of different spaces are never compared. A *document* is what a user put
in, read into passages. Callers never see a model: a collection records only the opaque id of its
space and the size of its vectors.
"""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from factotum.contracts.base import Contract
from factotum.contracts.identity import Owner
from factotum.contracts.ids import ChunkId, CollectionId, DocumentId, TenantId

LanguageCode = Annotated[str, Field(pattern=r"^[a-z]{2,3}$")]
"""An ISO 639 code in lower case: ``en``, ``it``..."""

MAX_DOCUMENT_TEXT = 5_000_000
MAX_QUERY_CHARS = 2000


class CollectionStatus(StrEnum):
    ACTIVE = "active"
    NEEDS_REINDEX = "needs_reindex"
    """The embedding model behind the owner's role is no longer the one that made the vectors:
    searching it by meaning would compare different spaces, so only its text is searched until it
    is indexed again."""

    REINDEXING = "reindexing"


class DocumentStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class CollectionOwner(StrEnum):
    """Who a new collection belongs to."""

    USER = "user"
    TENANT = "tenant"


class CollectionCreate(Contract):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    owner: CollectionOwner = CollectionOwner.USER


class CollectionUpdate(Contract):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _changes_something(self) -> Self:
        if self.name is None and self.description is None:
            raise ValueError("there is nothing to change")
        return self


class Collection(Contract):
    id: CollectionId
    tenant_id: TenantId
    owner: Owner
    name: str
    description: str = ""
    space_id: str
    dimensions: int = Field(ge=1)
    status: CollectionStatus = CollectionStatus.ACTIVE
    languages: tuple[LanguageCode, ...] = ()
    """The languages of the documents in it: what the text search is done in."""

    document_count: int = Field(default=0, ge=0)
    chunk_count: int = Field(default=0, ge=0)
    created_at: AwareDatetime
    updated_at: AwareDatetime


class CollectionList(Contract):
    """What listing collections answers: the ones the caller may see."""

    collections: tuple[Collection, ...] = ()


class DocumentText(Contract):
    """A document sent as plain text instead of as a file."""

    title: str = Field(min_length=1, max_length=300)
    text: str = Field(min_length=1, max_length=MAX_DOCUMENT_TEXT)
    language: LanguageCode | None = None


class Document(Contract):
    id: DocumentId
    collection_id: CollectionId
    title: str
    source: str | None = None
    """Where it came from, usually a file name. Never a path."""

    media_type: str
    size_bytes: int = Field(ge=0)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    language: LanguageCode | None = None
    language_declared: bool = False
    """True when the uploader said the language, false when it was detected."""

    status: DocumentStatus
    error_code: str | None = None
    error_message: str | None = None
    chunk_count: int = Field(default=0, ge=0)
    chunks_embedded: int = Field(default=0, ge=0)
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @property
    def progress(self) -> float:
        if self.status is DocumentStatus.READY:
            return 1.0
        if self.chunk_count == 0:
            return 0.0
        return min(1.0, self.chunks_embedded / self.chunk_count)


class DocumentList(Contract):
    """What listing the documents of a collection answers, newest first."""

    documents: tuple[Document, ...] = ()


class Location(Contract):
    """Where in a document a passage is, for a citation."""

    page: int | None = Field(default=None, ge=1)
    headings: tuple[str, ...] = ()


class Passage(Contract):
    """A piece of a document that answers a search."""

    chunk_id: ChunkId
    collection_id: CollectionId
    document_id: DocumentId
    document_title: str
    source: str | None = None
    text: str
    """The part that matched."""

    context: str
    """The section around it, for a reader that needs more than the match."""

    location: Location = Field(default_factory=Location)
    score: float
    """How high this passage came among the ones found: a fusion of ranks (ADR 0011), scaled by the
    best a rank can score. It says nothing of how well the passage answers: a question about nothing
    still has a first passage, at 1.0."""

    similarity: float | None = Field(default=None, ge=-1, le=1)
    """How alike the meaning of the query and of this passage are, as the embedding model measures
    it (the cosine, raw), for the best piece of its section. This is what says whether a passage is
    relevant (ADR 0028). None: only the words found it."""

    matched_by: tuple[Literal["vector", "text"], ...] = Field(min_length=1)
    language: LanguageCode | None = None
    untrusted: Literal[True] = True
    """A document is somebody else's words. Whatever it says, it is data and never an instruction:
    a turn that contains a passage has untrusted content in it (see ``authorize``)."""


class SearchRequest(Contract):
    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    collection_ids: tuple[CollectionId, ...] | None = Field(
        default=None, min_length=1, max_length=50
    )
    """Search only these collections. By default, every collection the caller may see."""

    limit: int = Field(default=8, ge=1, le=50)

    min_similarity: float | None = Field(default=None, ge=0, le=1)
    """Leave out what is not at least this alike in meaning to the query (``Passage.similarity``).
    Nothing is left out by default. What the words find alone is not relevant by this measure, with
    one exception: a query that names a code as written (``E-4021``, ``file.txt``), which the words
    find and a vector blurs (ADR 0028)."""


class SearchWarningCode(StrEnum):
    NEEDS_REINDEX = "needs_reindex"
    VECTOR_UNAVAILABLE = "vector_unavailable"
    EMBEDDING_NOT_CONFIGURED = "embedding_not_configured"


class SearchWarning(Contract):
    """Something that made a search less good than it could be, without making it fail."""

    collection_id: CollectionId
    code: SearchWarningCode
    message: str


class SearchResult(Contract):
    passages: tuple[Passage, ...] = ()
    warnings: tuple[SearchWarning, ...] = ()
    left_out: int = Field(default=0, ge=0)
    """How many sections that were found ``min_similarity`` left out for not being relevant."""
