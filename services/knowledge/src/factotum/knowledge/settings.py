"""Configuration of the knowledge service, read from the environment.

The database is the one of ``DatabaseSettings`` (``FACTOTUM_POSTGRES_*``, the names compose uses to
create it). Nothing here names a model, a provider or a key: those are the gateway's.
"""

from typing import Self

from pydantic import AnyHttpUrl, Field, model_validator

from factotum.db.settings import DatabaseSettings
from factotum.knowledge.chunker import ChunkingConfig
from factotum.knowledge.document import Limits
from factotum.knowledge.ingestion import IngestionSettings


class KnowledgeSettings(DatabaseSettings):
    knowledge_host: str = "127.0.0.1"
    """The interface to listen on. Loopback unless told otherwise: the service is for the other
    services and is not published outside the deployment."""

    knowledge_port: int = Field(default=8082, ge=1, le=65535)

    knowledge_gateway_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:8081")
    """Where the model gateway is: it is asked for every embedding."""

    knowledge_max_upload_mb: int = Field(default=25, ge=1, le=100)
    """The largest file that is accepted. The original is kept in the database."""

    knowledge_workers: int = Field(default=2, ge=0, le=16)
    """Documents read at the same time, in this process. 0 reads none: a process that only answers
    requests, while others do the work."""

    knowledge_child_tokens: int = Field(default=320, ge=64)
    """The size of a piece that is searched, in estimated tokens (breadcrumb included). Smaller for
    an embedding model with a small context."""

    knowledge_parent_tokens: int = Field(default=1200, ge=64)
    """The size of the section that comes back around a piece."""

    knowledge_embed_batch: int = Field(default=32, ge=1, le=256)
    """Pieces embedded in one request to the gateway."""

    knowledge_index_threshold: int = Field(default=2000, ge=1)
    """Pieces with a vector from which a collection gets a search index. Below it the search is
    exact, which is fast and never misses."""

    @model_validator(mode="after")
    def _the_pieces_make_sense(self) -> Self:
        ChunkingConfig(self.knowledge_child_tokens, self.knowledge_parent_tokens)
        return self

    @property
    def limits(self) -> Limits:
        return Limits(max_bytes=self.knowledge_max_upload_mb * 1024 * 1024)

    @property
    def ingestion(self) -> IngestionSettings:
        return IngestionSettings(
            limits=self.limits,
            chunking=ChunkingConfig(self.knowledge_child_tokens, self.knowledge_parent_tokens),
            batch=self.knowledge_embed_batch,
        )
