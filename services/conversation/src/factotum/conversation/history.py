"""Looking at what was said before, and keeping it findable (ADR 0012).

Nothing here runs unless a message refers to an earlier exchange. Searching the log is the same idea
as searching documents: words and meaning at once, fused by rank, degrading to words when there is
no embedding model. The vectors are made after the turn, in the background, so that a turn never
waits for them; each row remembers the space its vector lives in, and when the model behind an owner
changes the vectors of the old one are forgotten and made again.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Literal

from factotum.common.fusion import DEFAULT_K, reciprocal_rank_fusion
from factotum.contracts.conversation import (
    HistoryHit,
    Period,
    TurnWarning,
    WarningCode,
)
from factotum.contracts.errors import (
    ContextTooLongError,
    EmbeddingNotConfiguredError,
    FactotumError,
)
from factotum.contracts.identity import Owner
from factotum.contracts.ids import ExchangeId, TenantId
from factotum.conversation.periods import bounds
from factotum.conversation.store import ConversationStore, Found, Pending, Vector
from factotum.model_client import Embedder

logger = logging.getLogger("factotum.conversation")

EMBED_CHARS = 2000
"""How much of an exchange is embedded. What comes first says what it is about."""

_SMALLEST_TEXT = 200
_NOT_CONFIGURED_RETRY = timedelta(minutes=15)
_UNAVAILABLE_RETRY = timedelta(minutes=2)
_TOO_LONG_RETRY = timedelta(days=1)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Recall:
    """What a search of the log found, and what made it less good than it could be."""

    hits: tuple[HistoryHit, ...] = ()
    warnings: tuple[TurnWarning, ...] = ()


class History:
    def __init__(
        self,
        store: ConversationStore,
        embedder: Embedder,
        *,
        zone: tzinfo = UTC,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._zone = zone
        self._now = now

    async def search(
        self,
        tenant_id: TenantId,
        owner: Owner,
        *,
        query: str | None,
        when: Period | None,
        limit: int,
    ) -> Recall:
        """Earlier exchanges of ``owner``: those with words or a meaning near ``query``, within
        ``when`` if it says. Without words, the exchanges of the period, the latest first."""
        since, until = bounds(when, self._now(), self._zone) if when is not None else (None, None)
        if query is None:
            return Recall(await self._by_time(tenant_id, owner, since, until, limit))

        candidates = max(limit * 4, 20)
        by_words, (by_meaning, warnings) = await asyncio.gather(
            self._store.search_words(
                tenant_id, owner, query, since=since, until=until, limit=candidates
            ),
            self._by_meaning(tenant_id, owner, query, since, until, candidates),
        )
        hits = self._fused(by_words, by_meaning, limit)
        if not hits and when is not None:
            # The words did not find it, but the message says when: what was said then is what
            # there is to go by, and a model can tell which of it is meant.
            return Recall(await self._by_time(tenant_id, owner, since, until, limit), warnings)
        return Recall(hits, warnings)

    async def _by_time(
        self,
        tenant_id: TenantId,
        owner: Owner,
        since: datetime | None,
        until: datetime | None,
        limit: int,
    ) -> tuple[HistoryHit, ...]:
        found = await self._store.in_period(tenant_id, owner, since=since, until=until, limit=limit)
        return tuple(HistoryHit(exchange=e, score=1.0) for e in found)

    async def _by_meaning(
        self,
        tenant_id: TenantId,
        owner: Owner,
        query: str,
        since: datetime | None,
        until: datetime | None,
        limit: int,
    ) -> tuple[list[Found], tuple[TurnWarning, ...]]:
        try:
            embedded = await self._embedder.embed(owner, [query])
        except EmbeddingNotConfiguredError:
            return [], (
                _words_only(
                    "no embedding model is set up: earlier exchanges are searched by words only"
                ),
            )
        except FactotumError:
            return [], (
                _words_only(
                    "the embedding model did not answer: earlier exchanges are searched by "
                    "words only"
                ),
            )
        # What another model made is no use now: forget it, and it is made again in the background.
        await self._store.forget_other_spaces(tenant_id, owner, embedded.space_id)
        found = await self._store.search_vector(
            tenant_id,
            owner,
            embedded.space_id,
            embedded.vectors[0],
            since=since,
            until=until,
            limit=limit,
        )
        return found, ()

    @staticmethod
    def _fused(
        by_words: list[Found], by_meaning: list[Found], limit: int
    ) -> tuple[HistoryHit, ...]:
        rankings = [[f.exchange.id for f in by_words], [f.exchange.id for f in by_meaning]]
        exchanges = {f.exchange.id: f.exchange for f in [*by_words, *by_meaning]}
        found_by_words, found_by_meaning = set(rankings[0]), set(rankings[1])
        best = len(rankings) / (DEFAULT_K + 1)
        hits: list[HistoryHit] = []
        for identity, score in reciprocal_rank_fusion(rankings)[:limit]:
            matched: list[Literal["vector", "text"]] = []
            if identity in found_by_meaning:
                matched.append("vector")
            if identity in found_by_words:
                matched.append("text")
            hits.append(
                HistoryHit(
                    exchange=exchanges[identity],
                    score=min(1.0, score / best),
                    matched_by=tuple(matched),
                )
            )
        return tuple(hits)

    # ---- the vectors
    async def embed_pending(self, *, batch: int = 16) -> int:
        """Give some of the exchanges that have none a vector. Returns how many got one."""
        now = self._now()
        pending = await self._store.pending_embeddings(limit=batch, now=now)
        groups: dict[tuple[TenantId, Owner], list[Pending]] = {}
        for item in pending:
            groups.setdefault((item.tenant_id, item.owner), []).append(item)
        done = 0
        for (tenant_id, owner), items in groups.items():
            done += await self._embed_group(tenant_id, owner, items, now)
        return done

    async def _embed_group(
        self, tenant_id: TenantId, owner: Owner, items: list[Pending], now: datetime
    ) -> int:
        ids: list[ExchangeId] = [item.id for item in items]
        chars = EMBED_CHARS
        while True:
            texts = [f"{item.question}\n{item.answer}"[:chars] for item in items]
            try:
                embedded = await self._embedder.embed(owner, texts)
            except EmbeddingNotConfiguredError:
                await self._store.postpone_embeddings(ids, now + _NOT_CONFIGURED_RETRY)
                return 0
            except ContextTooLongError:
                if chars > _SMALLEST_TEXT:
                    chars //= 2
                    continue
                await self._store.postpone_embeddings(ids, now + _TOO_LONG_RETRY)
                return 0
            except FactotumError:
                await self._store.postpone_embeddings(ids, now + _UNAVAILABLE_RETRY)
                return 0
            await self._store.store_embeddings(
                [
                    Vector(item.id, tenant_id, embedded.space_id, vector)
                    for item, vector in zip(items, embedded.vectors, strict=True)
                ]
            )
            return len(items)


def _words_only(message: str) -> TurnWarning:
    return TurnWarning(code=WarningCode.HISTORY_WORDS_ONLY, message=message)


class Keeper:
    """The work in the background: give what was said its vector, and forget what is too old."""

    def __init__(
        self,
        history: History,
        store: ConversationStore,
        *,
        interval_s: float = 5.0,
        batch: int = 16,
        retention: timedelta | None = None,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._history = history
        self._store = store
        self._interval_s = interval_s
        self._batch = batch
        self._retention = retention
        self._now = now
        self._purged_at: datetime | None = None

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            worked = 0
            try:
                worked = await self.once()
            except Exception:  # a failing round is tried again, and must not end the service
                logger.exception("the history could not be kept")
            if worked >= self._batch:
                continue  # there is more waiting
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), self._interval_s)

    async def once(self) -> int:
        """One round: vectors for some exchanges, and the retention once an hour."""
        worked = await self._history.embed_pending(batch=self._batch)
        now = self._now()
        if self._retention is not None and (
            self._purged_at is None or now - self._purged_at >= timedelta(hours=1)
        ):
            self._purged_at = now
            forgotten = await self._store.purge_before(now - self._retention)
            if forgotten:
                logger.info("old conversations were forgotten", extra={"conversations": forgotten})
        return worked
