"""The store in memory: what the tests of everything above the database run on, and the model the
Postgres store is held to (the same contract tests run against both)."""

import math
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from factotum.contracts.conversation import Citation, Conversation, Exchange
from factotum.contracts.errors import ConversationNotFoundError
from factotum.contracts.identity import Owner
from factotum.contracts.ids import ConversationId, DeviceId, ExchangeId, TenantId
from factotum.conversation.store.base import Found, Pending, Vector


def tokens_of(text: str) -> list[str]:
    """The words of a text, in lower case and without accents: what "the same word" means."""
    folded = unicodedata.normalize("NFKD", text.casefold())
    return re.findall(r"\w+", "".join(c for c in folded if not unicodedata.combining(c)))


@dataclass(slots=True)
class _Conversation:
    id: ConversationId
    tenant_id: TenantId
    owner: Owner
    device_id: DeviceId | None
    started_at: datetime
    last_at: datetime


@dataclass(slots=True)
class _Row:
    exchange: Exchange
    tenant_id: TenantId
    owner: Owner
    vector: tuple[float, ...] | None = None
    space_id: str | None = None
    embed_after: datetime | None = None


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b, strict=True)) / norm if norm else 0.0


class InMemoryStore:
    def __init__(self) -> None:
        self._conversations: dict[ConversationId, _Conversation] = {}
        self._rows: dict[ExchangeId, _Row] = {}

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
        if conversation_id is not None:
            return self._conversation_view(self._owned(tenant_id, owner, conversation_id))
        active = [
            c
            for c in self._conversations.values()
            if (c.tenant_id, c.owner, c.device_id) == (tenant_id, owner, device_id)
            and c.last_at >= now - session_gap
        ]
        if active:
            return self._conversation_view(max(active, key=lambda c: c.last_at))
        made = _Conversation(ConversationId(uuid4()), tenant_id, owner, device_id, now, now)
        self._conversations[made.id] = made
        return self._conversation_view(made)

    async def list_conversations(
        self, tenant_id: TenantId, owner: Owner, *, limit: int, offset: int
    ) -> list[Conversation]:
        mine = [
            c
            for c in self._conversations.values()
            if (c.tenant_id, c.owner) == (tenant_id, owner) and self._exchanges_of(c.id)
        ]
        mine.sort(key=lambda c: c.last_at, reverse=True)
        return [self._conversation_view(c) for c in mine[offset : offset + limit]]

    async def get_conversation(
        self, tenant_id: TenantId, owner: Owner, conversation_id: ConversationId
    ) -> tuple[Conversation, list[Exchange]]:
        found = self._owned(tenant_id, owner, conversation_id)
        return self._conversation_view(found), self._exchanges_of(conversation_id)

    async def delete_conversation(
        self, tenant_id: TenantId, owner: Owner, conversation_id: ConversationId
    ) -> None:
        self._owned(tenant_id, owner, conversation_id)
        self._forget({conversation_id})

    async def delete_all(self, tenant_id: TenantId, owner: Owner) -> int:
        mine = {
            c.id
            for c in self._conversations.values()
            if (c.tenant_id, c.owner) == (tenant_id, owner)
        }
        self._forget(mine)
        return len(mine)

    async def purge_before(self, cutoff: datetime) -> int:
        old = {c.id for c in self._conversations.values() if c.last_at < cutoff}
        self._forget(old)
        return len(old)

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
        conversation = self._owned(tenant_id, owner, conversation_id)
        seq = len(self._exchanges_of(conversation_id)) + 1
        exchange = Exchange(
            id=exchange_id,
            conversation_id=conversation_id,
            seq=seq,
            created_at=now,
            question=question,
            answer=answer,
            citations=tuple(citations),
            untrusted=untrusted,
        )
        self._rows[exchange_id] = _Row(exchange, tenant_id, owner)
        conversation.last_at = max(conversation.last_at, now)
        return exchange

    async def has_exchanges(self, tenant_id: TenantId, owner: Owner) -> bool:
        return any(self._mine(tenant_id, owner))

    async def last_exchanges(
        self, tenant_id: TenantId, owner: Owner, conversation_id: ConversationId, *, limit: int
    ) -> list[Exchange]:
        self._owned(tenant_id, owner, conversation_id)
        return self._exchanges_of(conversation_id)[-limit:]

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
        inside = [row.exchange for row in self._mine(tenant_id, owner, since, until)]
        inside.sort(key=lambda e: e.created_at, reverse=True)
        return inside[:limit]

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
        wanted = set(tokens_of(query))
        found: list[Found] = []
        for row in self._mine(tenant_id, owner, since, until):
            shared = wanted & set(tokens_of(f"{row.exchange.question} {row.exchange.answer}"))
            if shared:
                found.append(Found(row.exchange, float(len(shared))))
        found.sort(key=lambda f: (-f.score, -f.exchange.created_at.timestamp()))
        return found[:limit]

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
        found = [
            Found(row.exchange, _cosine(vector, row.vector))
            for row in self._mine(tenant_id, owner, since, until)
            if row.vector is not None and row.space_id == space_id
        ]
        found.sort(key=lambda f: (-f.score, -f.exchange.created_at.timestamp()))
        return found[:limit]

    async def forget_other_spaces(self, tenant_id: TenantId, owner: Owner, space_id: str) -> int:
        forgotten = 0
        for row in self._mine(tenant_id, owner):
            if row.space_id is not None and row.space_id != space_id:
                row.vector = row.space_id = row.embed_after = None
                forgotten += 1
        return forgotten

    # ---- the vectors
    async def pending_embeddings(self, *, limit: int, now: datetime) -> list[Pending]:
        waiting = [
            row
            for row in self._rows.values()
            if row.vector is None and (row.embed_after is None or row.embed_after <= now)
        ]
        waiting.sort(key=lambda row: row.exchange.created_at)
        return [
            Pending(
                row.exchange.id,
                row.tenant_id,
                row.owner,
                row.exchange.question,
                row.exchange.answer,
            )
            for row in waiting[:limit]
        ]

    async def store_embeddings(self, vectors: Sequence[Vector]) -> None:
        for made in vectors:
            row = self._rows.get(made.id)
            if row is not None and row.tenant_id == made.tenant_id:
                row.vector, row.space_id, row.embed_after = made.vector, made.space_id, None

    async def postpone_embeddings(self, ids: Sequence[ExchangeId], until: datetime) -> None:
        for identity in ids:
            row = self._rows.get(identity)
            if row is not None:
                row.embed_after = until

    # ---- helpers
    def _owned(
        self, tenant_id: TenantId, owner: Owner, conversation_id: ConversationId
    ) -> _Conversation:
        found = self._conversations.get(conversation_id)
        if found is None or (found.tenant_id, found.owner) != (tenant_id, owner):
            raise ConversationNotFoundError("the conversation does not exist")
        return found

    def _mine(
        self,
        tenant_id: TenantId,
        owner: Owner,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[_Row]:
        return [
            row
            for row in self._rows.values()
            if (row.tenant_id, row.owner) == (tenant_id, owner)
            and (since is None or row.exchange.created_at >= since)
            and (until is None or row.exchange.created_at < until)
        ]

    def _exchanges_of(self, conversation_id: ConversationId) -> list[Exchange]:
        inside = [
            r.exchange for r in self._rows.values() if r.exchange.conversation_id == conversation_id
        ]
        return sorted(inside, key=lambda e: e.seq)

    def _conversation_view(self, conversation: _Conversation) -> Conversation:
        return Conversation(
            id=conversation.id,
            started_at=conversation.started_at,
            last_at=conversation.last_at,
            device_id=conversation.device_id,
            exchanges=len(self._exchanges_of(conversation.id)),
        )

    def _forget(self, conversations: set[ConversationId]) -> None:
        for identity in conversations:
            self._conversations.pop(identity, None)
        for key in [
            k for k, r in self._rows.items() if r.exchange.conversation_id in conversations
        ]:
            del self._rows[key]
