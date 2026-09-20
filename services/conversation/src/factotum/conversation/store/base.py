"""What the service keeps: conversations, and the exchanges in them.

Everything is scoped by a tenant and by an *owner*, the subject a turn acted for (a user, or the
tenant for a shared device): what one owner saved, another never sees, and asking for it is answered
as if it were not there. The store searches what it keeps by words and by vectors; making the
vectors, and fusing the two rankings, is the history service's business.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from factotum.contracts.conversation import Citation, Conversation, Exchange
from factotum.contracts.identity import Owner
from factotum.contracts.ids import ConversationId, DeviceId, ExchangeId, TenantId


@dataclass(frozen=True, slots=True)
class Found:
    """An exchange a search found, with a score that only means something inside its own ranking."""

    exchange: Exchange
    score: float


@dataclass(frozen=True, slots=True)
class Pending:
    """An exchange whose vector has yet to be made."""

    id: ExchangeId
    tenant_id: TenantId
    owner: Owner
    question: str
    answer: str


@dataclass(frozen=True, slots=True)
class Vector:
    """The vector of an exchange, and the space it lives in."""

    id: ExchangeId
    tenant_id: TenantId
    space_id: str
    vector: tuple[float, ...]


class ConversationStore(Protocol):
    # ---- conversations
    async def open_conversation(
        self,
        tenant_id: TenantId,
        owner: Owner,
        *,
        device_id: DeviceId | None,
        conversation_id: ConversationId | None,
        now: datetime,
        session_gap: timedelta,
    ) -> Conversation:
        """The conversation a turn belongs to. One that is named must be the owner's, or
        ``ConversationNotFoundError``. Without a name, it is the owner's conversation on this device
        that was active within ``session_gap`` of ``now``, or a new one."""
        ...

    async def list_conversations(
        self, tenant_id: TenantId, owner: Owner, *, limit: int, offset: int
    ) -> list[Conversation]:
        """The owner's conversations, the last active first."""
        ...

    async def get_conversation(
        self, tenant_id: TenantId, owner: Owner, conversation_id: ConversationId
    ) -> tuple[Conversation, list[Exchange]]:
        """A conversation and its exchanges in the order they took place."""
        ...

    async def delete_conversation(
        self, tenant_id: TenantId, owner: Owner, conversation_id: ConversationId
    ) -> None: ...

    async def delete_all(self, tenant_id: TenantId, owner: Owner) -> int:
        """Forget everything the owner said. Returns how many conversations there were."""
        ...

    async def purge_before(self, cutoff: datetime) -> int:
        """Forget every conversation, of anybody, whose last exchange is older than ``cutoff``.
        Returns how many. It is retention, and belongs to nobody in particular."""
        ...

    async def purge_shared(self, cutoff: datetime) -> int:
        """Forget what was said on shared devices (whose owner is a tenant) before ``cutoff``, and
        the conversations that leaves empty. Returns how many exchanges. What a shared device says
        is kept only as long as a shared device can see it, which is not long."""
        ...

    # ---- exchanges
    async def add_exchange(
        self,
        tenant_id: TenantId,
        owner: Owner,
        *,
        conversation_id: ConversationId,
        exchange_id: ExchangeId,
        question: str,
        answer: str,
        citations: Sequence[Citation],
        untrusted: bool,
        now: datetime,
    ) -> Exchange:
        """Save an exchange at the end of a conversation of the owner's."""
        ...

    async def has_exchanges(self, tenant_id: TenantId, owner: Owner) -> bool: ...

    async def last_exchanges(
        self,
        tenant_id: TenantId,
        owner: Owner,
        conversation_id: ConversationId,
        *,
        limit: int,
        since: datetime | None = None,
    ) -> list[Exchange]:
        """The last ``limit`` exchanges of a conversation, oldest first; only those from ``since``
        on when it is given."""
        ...

    # ---- searching what was said
    async def in_period(
        self,
        tenant_id: TenantId,
        owner: Owner,
        *,
        since: datetime | None,
        until: datetime | None,
        limit: int,
    ) -> list[Exchange]:
        """Exchanges from ``since`` (included) to ``until`` (not), the latest first."""
        ...

    async def search_words(
        self,
        tenant_id: TenantId,
        owner: Owner,
        query: str,
        *,
        since: datetime | None,
        until: datetime | None,
        limit: int,
    ) -> list[Found]:
        """Exchanges with some of the words of ``query`` (accents and capitals ignored, the words
        OR-ed), the best first."""
        ...

    async def search_vector(
        self,
        tenant_id: TenantId,
        owner: Owner,
        space_id: str,
        vector: Sequence[float],
        *,
        since: datetime | None,
        until: datetime | None,
        limit: int,
    ) -> list[Found]:
        """Exchanges nearest to ``vector`` among those whose own vector is in ``space_id``."""
        ...

    async def forget_other_spaces(self, tenant_id: TenantId, owner: Owner, space_id: str) -> int:
        """The embedding model behind the owner is not the one that made some vectors: forget them,
        so that they are made again. Returns how many."""
        ...

    # ---- the vectors
    async def pending_embeddings(self, *, limit: int, now: datetime) -> list[Pending]:
        """Exchanges without a vector that are not being left for later, the oldest first. What was
        said on a shared device is never given one: it is not searched, only looked back at."""
        ...

    async def store_embeddings(self, vectors: Sequence[Vector]) -> None: ...

    async def postpone_embeddings(self, ids: Sequence[ExchangeId], until: datetime) -> None:
        """Leave these for later: the owner has no embedding model, or it did not answer."""
        ...
