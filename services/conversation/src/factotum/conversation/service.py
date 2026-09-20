"""What the conversation service offers, and who may ask for it.

Anybody who is let in may talk: a turn is answered, and remembered, for the subject the caller acts
for (a person, or the tenant for a shared device). Reading what was said, and forgetting it, is
another matter: it is for a person, about their own conversations. A shared device can be spoken to
and remembered but is never a way to read or delete what the household said (ADR 0004:
administration does not go through a device); that will be managed from the hub.
"""

from collections.abc import AsyncGenerator

from factotum.contracts.conversation import (
    ConversationDetail,
    ConversationList,
    HistoryQuery,
    HistoryResult,
    TurnEvent,
    TurnRequest,
)
from factotum.contracts.errors import PolicyViolationError
from factotum.contracts.identity import Owner, Principal, SubjectKind
from factotum.contracts.ids import ConversationId
from factotum.conversation.history import History
from factotum.conversation.store import ConversationStore
from factotum.conversation.turn import TurnEngine


def _person(principal: Principal) -> Owner:
    """The owner of what a person may read and forget: their own."""
    if principal.acts_as.kind is not SubjectKind.USER:
        raise PolicyViolationError(
            "a shared device can be spoken to and remembered, but only a person can read or "
            "forget what was said"
        )
    return Owner.of_subject(principal.acts_as)


class ConversationService:
    def __init__(self, store: ConversationStore, engine: TurnEngine, history: History) -> None:
        self._store = store
        self._engine = engine
        self._history = history

    def turn(self, principal: Principal, request: TurnRequest) -> AsyncGenerator[TurnEvent]:
        """A turn, as the events it streams. What makes it impossible is raised when the first is
        asked for."""
        return self._engine.run(principal, request)

    async def list_conversations(
        self, principal: Principal, *, limit: int, offset: int
    ) -> ConversationList:
        owner = _person(principal)
        found = await self._store.list_conversations(
            principal.tenant_id, owner, limit=limit, offset=offset
        )
        return ConversationList(conversations=tuple(found))

    async def get_conversation(
        self, principal: Principal, conversation_id: ConversationId
    ) -> ConversationDetail:
        owner = _person(principal)
        conversation, exchanges = await self._store.get_conversation(
            principal.tenant_id, owner, conversation_id
        )
        return ConversationDetail(conversation=conversation, exchanges=tuple(exchanges))

    async def delete_conversation(
        self, principal: Principal, conversation_id: ConversationId
    ) -> None:
        await self._store.delete_conversation(
            principal.tenant_id, _person(principal), conversation_id
        )

    async def forget_everything(self, principal: Principal) -> int:
        """Forget every conversation of the caller's. Returns how many there were."""
        return await self._store.delete_all(principal.tenant_id, _person(principal))

    async def search_history(self, principal: Principal, query: HistoryQuery) -> HistoryResult:
        owner = _person(principal)
        recall = await self._history.search(
            principal.tenant_id,
            owner,
            query=query.query,
            when=query.when,
            limit=query.limit,
        )
        return HistoryResult(hits=recall.hits, warnings=recall.warnings)
