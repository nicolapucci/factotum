"""Typed identifiers: distinct types for distinct kinds of id, all backed by UUIDs.

The type checker refuses to pass a ``DeviceId`` where a ``TenantId`` is expected, which removes a
whole class of mix-ups between services.
"""

from typing import NewType
from uuid import UUID

TenantId = NewType("TenantId", UUID)
UserId = NewType("UserId", UUID)
DeviceId = NewType("DeviceId", UUID)
SpaceId = NewType("SpaceId", UUID)
SkillId = NewType("SkillId", UUID)
BindingId = NewType("BindingId", UUID)
ApprovalId = NewType("ApprovalId", UUID)
AuditId = NewType("AuditId", UUID)
ModelBindingId = NewType("ModelBindingId", UUID)
CollectionId = NewType("CollectionId", UUID)
DocumentId = NewType("DocumentId", UUID)
ChunkId = NewType("ChunkId", UUID)
ConversationId = NewType("ConversationId", UUID)
ExchangeId = NewType("ExchangeId", UUID)
