"""Storage of collections, documents and pieces."""

from factotum.knowledge.store.base import (
    Hit,
    Job,
    JobKind,
    KnowledgeStore,
    NewDocument,
    PendingChunk,
)
from factotum.knowledge.store.memory import InMemoryStore

__all__ = [
    "Hit",
    "InMemoryStore",
    "Job",
    "JobKind",
    "KnowledgeStore",
    "NewDocument",
    "PendingChunk",
]
