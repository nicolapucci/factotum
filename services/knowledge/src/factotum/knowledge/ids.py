"""Identifiers that follow from what they name.

A document read again gives the same pieces with the same ids, so reading it twice (a job that ran
twice, a document read again after a crash) leaves one set of pieces, not two.
"""

from uuid import UUID, uuid5

from factotum.contracts.ids import ChunkId, DocumentId

_NAMESPACE = UUID("6f3c2d1e-7b1a-4c53-9a63-0d0c5b7e4f11")


def parent_id(document_id: DocumentId, ordinal: int) -> UUID:
    return uuid5(_NAMESPACE, f"{document_id}/parent/{ordinal}")


def chunk_id(document_id: DocumentId, ordinal: int) -> ChunkId:
    return ChunkId(uuid5(_NAMESPACE, f"{document_id}/chunk/{ordinal}"))
