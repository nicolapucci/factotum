"""The store in Postgres, in the schema ``conversation``.

What is searched is small and belongs to one owner (the exchanges of one person, or of a household),
so the search by meaning is exact, without an index: it reads that owner's vectors and no others,
which is fast into the tens of thousands of exchanges. An index comes when that stops being true.
Every query filters by tenant, and every transaction says the tenant to the database.
"""

import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import asyncpg

from factotum.contracts.conversation import Citation, Conversation, Exchange
from factotum.contracts.errors import ConversationNotFoundError
from factotum.contracts.identity import Owner
from factotum.contracts.ids import ConversationId, DeviceId, ExchangeId, TenantId
from factotum.conversation.store.base import Found, Pending, Vector
from factotum.db.migrations import load_migrations, migrate
from factotum.db.pool import Database

SCHEMA = "conversation"
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"

_WORDS = "conversation.words"

_CONVERSATION = (
    "SELECT c.id, c.started_at, c.last_at, c.device_id, "
    "(SELECT count(*) FROM conversation.exchanges e WHERE e.conversation_id = c.id) AS exchanges "
    "FROM conversation.conversations c"
)
_OWNED = "c.tenant_id = $1 AND c.owner_kind = $2 AND c.owner_id = $3"
_SAID_SOMETHING = "EXISTS (SELECT 1 FROM conversation.exchanges x WHERE x.conversation_id = c.id)"

_EXCHANGE = (
    "e.id, e.conversation_id, e.seq, e.created_at, e.question, e.answer, e.citations, e.untrusted"
)
_MINE = "e.tenant_id = $1 AND e.owner_kind = $2 AND e.owner_id = $3"
_WITHIN = (
    "($4::timestamptz IS NULL OR e.created_at >= $4) "
    "AND ($5::timestamptz IS NULL OR e.created_at < $5)"
)

IN_PERIOD_SQL = (
    f"SELECT {_EXCHANGE} FROM conversation.exchanges e "  # noqa: S608 - constants only
    f"WHERE {_MINE} AND {_WITHIN} ORDER BY e.created_at DESC LIMIT $6"
)

WORDS_SQL = (
    f"WITH q AS (SELECT replace(plainto_tsquery('{_WORDS}'::regconfig, $6)::text, ' & ', ' | ')"  # noqa: S608
    "::tsquery AS query) "
    f"SELECT {_EXCHANGE}, ts_rank(e.tsv, q.query) AS score "
    "FROM conversation.exchanges e CROSS JOIN q "
    f"WHERE {_MINE} AND {_WITHIN} AND e.tsv @@ q.query "
    "ORDER BY score DESC, e.created_at DESC LIMIT $7"
)

DELETE_ONE_SQL = f"DELETE FROM conversation.conversations c WHERE c.id = $4 AND {_OWNED}"  # noqa: S608
LOCK_ONE_SQL = (
    f"SELECT c.id FROM conversation.conversations c WHERE c.id = $4 AND {_OWNED} FOR UPDATE"  # noqa: S608
)
INSERT_SQL = (
    "INSERT INTO conversation.exchanges (id, tenant_id, conversation_id, owner_kind, "  # noqa: S608
    "owner_id, seq, created_at, question, answer, citations, untrusted, tsv) "
    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11, "
    f"to_tsvector('{_WORDS}'::regconfig, $8 || ' ' || $9))"
)
FORGET_OTHER_SPACES_SQL = (
    "UPDATE conversation.exchanges e SET embedding = NULL, space_id = NULL, embed_after = NULL "  # noqa: S608
    f"WHERE {_MINE} AND e.space_id IS NOT NULL AND e.space_id <> $4"
)


def vector_text(vector: Sequence[float]) -> str:
    """A vector the way Postgres reads it as text."""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def vector_sql(dimensions: int) -> str:
    """The search by meaning, exact, among the owner's exchanges whose vector is in a given space.
    Parameters: tenant, owner kind, owner id, since, until, the vector as text, the space, and
    how many."""
    distance = f"e.embedding::vector({dimensions}) <=> $6::text::vector({dimensions})"
    return (
        f"SELECT {_EXCHANGE}, 1 - ({distance}) AS score "  # noqa: S608 - the size is an integer
        "FROM conversation.exchanges e "
        f"WHERE {_MINE} AND {_WITHIN} AND e.space_id = $7 AND e.embedding IS NOT NULL "
        f"ORDER BY {distance} LIMIT $8"
    )


async def prepare(database: Database) -> list[int]:
    """Bring the tables of the service up to date. Returns the migrations that were applied."""
    async with database.connection() as connection:
        return await migrate(connection, SCHEMA, load_migrations(MIGRATIONS))


def _conversation(row: "asyncpg.Record") -> Conversation:
    return Conversation(
        id=ConversationId(row["id"]),
        started_at=row["started_at"],
        last_at=row["last_at"],
        device_id=None if row["device_id"] is None else DeviceId(row["device_id"]),
        exchanges=row["exchanges"],
    )


def _exchange(row: "asyncpg.Record") -> Exchange:
    return Exchange(
        id=ExchangeId(row["id"]),
        conversation_id=ConversationId(row["conversation_id"]),
        seq=row["seq"],
        created_at=row["created_at"],
        question=row["question"],
        answer=row["answer"],
        citations=tuple(Citation.model_validate(c) for c in json.loads(row["citations"])),
        untrusted=row["untrusted"],
    )


def _count(status: str) -> int:
    """How many rows an ``UPDATE`` or ``DELETE`` says it touched: ``"DELETE 3"``."""
    return int(status.rsplit(" ", 1)[-1])


class PostgresStore:
    def __init__(self, database: Database) -> None:
        self._db = database

    @property
    def database(self) -> Database:
        return self._db

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
        async with self._db.transaction(tenant_id) as connection:
            if conversation_id is not None:
                row = await connection.fetchrow(
                    f"{_CONVERSATION} WHERE c.id = $4 AND {_OWNED}",
                    tenant_id,
                    owner.kind.value,
                    owner.id,
                    conversation_id,
                )
                if row is None:
                    raise ConversationNotFoundError("the conversation does not exist")
                return _conversation(row)
            # Two turns that begin together on one device are in one conversation, not in two.
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"conversation:{tenant_id}:{owner.kind.value}:{owner.id}:{device_id}",
            )
            row = await connection.fetchrow(
                f"{_CONVERSATION} WHERE {_OWNED} AND c.device_id IS NOT DISTINCT FROM $4 "
                "AND c.last_at >= $5 ORDER BY c.last_at DESC LIMIT 1",
                tenant_id,
                owner.kind.value,
                owner.id,
                device_id,
                now - session_gap,
            )
            if row is not None:
                return _conversation(row)
            made = ConversationId(uuid4())
            await connection.execute(
                "INSERT INTO conversation.conversations "
                "(id, tenant_id, owner_kind, owner_id, device_id, started_at, last_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $6)",
                made,
                tenant_id,
                owner.kind.value,
                owner.id,
                device_id,
                now,
            )
            return Conversation(
                id=made, started_at=now, last_at=now, device_id=device_id, exchanges=0
            )

    async def list_conversations(
        self, tenant_id: TenantId, owner: Owner, *, limit: int, offset: int
    ) -> list[Conversation]:
        async with self._db.transaction(tenant_id) as connection:
            rows = await connection.fetch(
                f"{_CONVERSATION} WHERE {_OWNED} AND {_SAID_SOMETHING} "
                "ORDER BY c.last_at DESC LIMIT $4 OFFSET $5",
                tenant_id,
                owner.kind.value,
                owner.id,
                limit,
                offset,
            )
        return [_conversation(row) for row in rows]

    async def get_conversation(
        self, tenant_id: TenantId, owner: Owner, conversation_id: ConversationId
    ) -> tuple[Conversation, list[Exchange]]:
        async with self._db.transaction(tenant_id) as connection:
            row = await connection.fetchrow(
                f"{_CONVERSATION} WHERE c.id = $4 AND {_OWNED}",
                tenant_id,
                owner.kind.value,
                owner.id,
                conversation_id,
            )
            if row is None:
                raise ConversationNotFoundError("the conversation does not exist")
            rows = await connection.fetch(
                f"SELECT {_EXCHANGE} FROM conversation.exchanges e "  # noqa: S608
                "WHERE e.conversation_id = $1 ORDER BY e.seq",
                conversation_id,
            )
        return _conversation(row), [_exchange(r) for r in rows]

    async def delete_conversation(
        self, tenant_id: TenantId, owner: Owner, conversation_id: ConversationId
    ) -> None:
        async with self._db.transaction(tenant_id) as connection:
            status = await connection.execute(
                DELETE_ONE_SQL,
                tenant_id,
                owner.kind.value,
                owner.id,
                conversation_id,
            )
        if _count(status) == 0:
            raise ConversationNotFoundError("the conversation does not exist")

    async def delete_all(self, tenant_id: TenantId, owner: Owner) -> int:
        async with self._db.transaction(tenant_id) as connection:
            status = await connection.execute(
                f"DELETE FROM conversation.conversations c WHERE {_OWNED}",  # noqa: S608
                tenant_id,
                owner.kind.value,
                owner.id,
            )
        return _count(status)

    async def purge_before(self, cutoff: datetime) -> int:
        async with self._db.connection() as connection:
            status = await connection.execute(
                "DELETE FROM conversation.conversations WHERE last_at < $1", cutoff
            )
        return _count(status)

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
        async with self._db.transaction(tenant_id) as connection:
            locked = await connection.fetchval(
                LOCK_ONE_SQL,
                tenant_id,
                owner.kind.value,
                owner.id,
                conversation_id,
            )
            if locked is None:
                raise ConversationNotFoundError("the conversation does not exist")
            seq = await connection.fetchval(
                "SELECT coalesce(max(seq), 0) + 1 FROM conversation.exchanges "
                "WHERE conversation_id = $1",
                conversation_id,
            )
            kept: tuple[Citation, ...] = tuple(citations)
            await connection.execute(
                INSERT_SQL,
                exchange_id,
                tenant_id,
                conversation_id,
                owner.kind.value,
                owner.id,
                seq,
                now,
                question,
                answer,
                json.dumps([c.model_dump(mode="json") for c in kept]),
                untrusted,
            )
            await connection.execute(
                "UPDATE conversation.conversations SET last_at = greatest(last_at, $2) "
                "WHERE id = $1",
                conversation_id,
                now,
            )
        return Exchange(
            id=exchange_id,
            conversation_id=conversation_id,
            seq=seq,
            created_at=now,
            question=question,
            answer=answer,
            citations=kept,
            untrusted=untrusted,
        )

    async def has_exchanges(self, tenant_id: TenantId, owner: Owner) -> bool:
        async with self._db.transaction(tenant_id) as connection:
            return bool(
                await connection.fetchval(
                    f"SELECT EXISTS (SELECT 1 FROM conversation.exchanges e WHERE {_MINE})",  # noqa: S608
                    tenant_id,
                    owner.kind.value,
                    owner.id,
                )
            )

    async def last_exchanges(
        self, tenant_id: TenantId, owner: Owner, conversation_id: ConversationId, *, limit: int
    ) -> list[Exchange]:
        async with self._db.transaction(tenant_id) as connection:
            owned = await connection.fetchval(
                f"SELECT c.id FROM conversation.conversations c WHERE c.id = $4 AND {_OWNED}",  # noqa: S608
                tenant_id,
                owner.kind.value,
                owner.id,
                conversation_id,
            )
            if owned is None:
                raise ConversationNotFoundError("the conversation does not exist")
            rows = await connection.fetch(
                f"SELECT {_EXCHANGE} FROM conversation.exchanges e "  # noqa: S608
                "WHERE e.conversation_id = $1 ORDER BY e.seq DESC LIMIT $2",
                conversation_id,
                limit,
            )
        return [_exchange(row) for row in reversed(rows)]

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
        async with self._db.transaction(tenant_id) as connection:
            rows = await connection.fetch(
                IN_PERIOD_SQL, tenant_id, owner.kind.value, owner.id, since, until, limit
            )
        return [_exchange(row) for row in rows]

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
        async with self._db.transaction(tenant_id) as connection:
            rows = await connection.fetch(
                WORDS_SQL, tenant_id, owner.kind.value, owner.id, since, until, query, limit
            )
        return [Found(_exchange(row), float(row["score"])) for row in rows]

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
        async with self._db.transaction(tenant_id) as connection:
            rows = await connection.fetch(
                vector_sql(len(vector)),
                tenant_id,
                owner.kind.value,
                owner.id,
                since,
                until,
                vector_text(vector),
                space_id,
                limit,
            )
        return [Found(_exchange(row), float(row["score"])) for row in rows]

    async def forget_other_spaces(self, tenant_id: TenantId, owner: Owner, space_id: str) -> int:
        async with self._db.transaction(tenant_id) as connection:
            status = await connection.execute(
                FORGET_OTHER_SPACES_SQL,
                tenant_id,
                owner.kind.value,
                owner.id,
                space_id,
            )
        return _count(status)

    # ---- the vectors
    async def pending_embeddings(self, *, limit: int, now: datetime) -> list[Pending]:
        async with self._db.connection() as connection:
            rows = await connection.fetch(
                "SELECT id, tenant_id, owner_kind, owner_id, question, answer "
                "FROM conversation.exchanges WHERE embedding IS NULL "
                "AND (embed_after IS NULL OR embed_after <= $1) ORDER BY created_at LIMIT $2",
                now,
                limit,
            )
        return [
            Pending(
                id=ExchangeId(row["id"]),
                tenant_id=TenantId(row["tenant_id"]),
                owner=Owner.model_validate({"kind": row["owner_kind"], "id": row["owner_id"]}),
                question=row["question"],
                answer=row["answer"],
            )
            for row in rows
        ]

    async def store_embeddings(self, vectors: Sequence[Vector]) -> None:
        if not vectors:
            return
        async with self._db.connection() as connection:
            await connection.execute(
                "UPDATE conversation.exchanges e SET embedding = t.embedding::vector, "
                "space_id = t.space_id, embed_after = NULL "
                "FROM unnest($1::uuid[], $2::uuid[], $3::text[], $4::text[]) "
                "AS t(id, tenant_id, space_id, embedding) "
                "WHERE e.id = t.id AND e.tenant_id = t.tenant_id",
                [v.id for v in vectors],
                [v.tenant_id for v in vectors],
                [v.space_id for v in vectors],
                [vector_text(v.vector) for v in vectors],
            )

    async def postpone_embeddings(self, ids: Sequence[ExchangeId], until: datetime) -> None:
        if not ids:
            return
        async with self._db.connection() as connection:
            await connection.execute(
                "UPDATE conversation.exchanges SET embed_after = $2 WHERE id = ANY($1::uuid[])",
                list(ids),
                until,
            )
