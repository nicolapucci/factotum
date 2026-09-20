"""One turn: a message in, an answer streamed out, and the exchange saved (ADR 0012).

A turn carries no history. It searches the user's documents for what answers the message, and asks
the model. Only when a small model, reading the message alone, says that it refers to an earlier
exchange does the turn look at what was said before: the last exchanges for a continuation, a search
of the log for something said earlier. The search of the documents starts at once, beside that
question, and is thrown away when the answer to it says the message was to be read together with an
earlier exchange.

What goes wrong is met without giving up: a model that cannot say whether the message refers to the
past, documents that do not answer, an embedding model that is away. The turn goes on with what it
has and says so in a warning. What cannot be got over (no chat model, a message the model cannot
take) is raised before the first event, so that the caller can answer with the right status; what
fails after that ends the stream with a ``Failed`` event, and saves nothing.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from factotum.contracts.chat import (
    ChatRequest,
    ErrorEvent,
    FinishEvent,
    FinishReason,
    TextDelta,
    Usage,
    UsageEvent,
)
from factotum.contracts.conversation import (
    MAX_HISTORY_QUERY_CHARS,
    Cited,
    Delta,
    Exchange,
    Failed,
    Finished,
    Phase,
    Reference,
    ReferenceKind,
    Started,
    Status,
    TurnEvent,
    TurnRequest,
    TurnWarning,
    WarningCode,
)
from factotum.contracts.errors import FactotumError
from factotum.contracts.identity import Owner, Principal
from factotum.contracts.ids import ConversationId, ExchangeId
from factotum.contracts.knowledge import Passage
from factotum.contracts.models import ModelRole, Profile
from factotum.conversation.context import PromptBuilder, citations_of
from factotum.conversation.documents import DocumentSearch
from factotum.conversation.gate import ReferenceGate
from factotum.conversation.history import History
from factotum.conversation.store import ConversationStore
from factotum.model_client import Chat

logger = logging.getLogger("factotum.conversation")

Found = tuple[tuple[Passage, ...], TurnWarning | None]
"""What searching the documents came to: the passages, and a warning if it could not be done."""


@dataclass(frozen=True, slots=True)
class TurnSettings:
    recent_exchanges: int = 3
    """How many of the last exchanges a continuation brings back."""

    history_hits: int = 3
    """How many exchanges a reference to something earlier brings back."""

    document_passages: int = 6
    """How many passages of the documents are asked for."""

    knowledge_timeout_s: float = 4.0
    """How long the documents are waited for before the answer goes on without them."""

    session_gap: timedelta = field(default_factory=lambda: timedelta(minutes=30))
    """How long a conversation stays the one a message without a name belongs to."""

    profile_ttl: timedelta = field(default_factory=lambda: timedelta(seconds=60))
    """How long what a chat model takes in is remembered before it is asked again."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


class TurnEngine:
    def __init__(
        self,
        store: ConversationStore,
        chat: Chat,
        documents: DocumentSearch,
        history: History,
        gate: ReferenceGate,
        builder: PromptBuilder,
        *,
        settings: TurnSettings | None = None,
        now: Callable[[], datetime] = _utc_now,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._store = store
        self._chat = chat
        self._documents = documents
        self._history = history
        self._gate = gate
        self._builder = builder
        self._settings = settings or TurnSettings()
        self._now = now
        self._new_id = new_id
        self._profiles: dict[Owner, tuple[datetime, Profile]] = {}

    async def run(self, principal: Principal, request: TurnRequest) -> AsyncGenerator[TurnEvent]:
        """The events of a turn. What makes it impossible to answer at all is raised when the first
        event is asked for; a failure after that is the last event, ``Failed``."""
        owner = Owner.of_subject(principal.acts_as)
        now = self._now()
        conversation = await self._store.open_conversation(
            principal.tenant_id,
            owner,
            device_id=principal.device_id,
            conversation_id=request.conversation_id,
            now=now,
            session_gap=self._settings.session_gap,
        )
        profile = await self._profile(owner)
        # Whether the instructions and the message alone fit is known now, before anything is sent.
        self._builder.build(
            message=request.message,
            passages=(),
            earlier=(),
            now=now,
            max_input_tokens=profile.max_input_tokens,
        )
        exchange_id = ExchangeId(self._new_id())
        yield Started(conversation_id=conversation.id, exchange_id=exchange_id)

        turn = self._turn(principal, owner, conversation.id, request.message, exchange_id, profile)
        try:
            async for event in turn:
                yield event
        except FactotumError as error:
            logger.warning("a turn failed", extra={"code": error.code})
            yield Failed(code=error.code, message=error.message)
        finally:
            await turn.aclose()

    async def _profile(self, owner: Owner) -> Profile:
        now = self._now()
        cached = self._profiles.get(owner)
        if cached is not None and now - cached[0] < self._settings.profile_ttl:
            return cached[1]
        profile = await self._chat.profile(owner, ModelRole.CHAT_MAIN)
        self._profiles[owner] = (now, profile)
        return profile

    async def _turn(
        self,
        principal: Principal,
        owner: Owner,
        conversation_id: ConversationId,
        message: str,
        exchange_id: ExchangeId,
        profile: Profile,
    ) -> AsyncGenerator[TurnEvent]:
        tenant = principal.tenant_id
        settings = self._settings
        searching: asyncio.Task[Found] | None = asyncio.create_task(self._passages(owner, message))
        try:
            reference = Reference(kind=ReferenceKind.NONE)
            if await self._store.has_exchanges(tenant, owner):
                yield Status(phase=Phase.UNDERSTANDING)
                try:
                    reference = await self._gate.decide(owner, message)
                except FactotumError as error:
                    logger.warning("a message could not be read", extra={"code": error.code})
                    yield TurnWarning(
                        code=WarningCode.REFERENCE_UNAVAILABLE,
                        message="it could not be told whether the message refers to an earlier "
                        "exchange: the answer does not use any",
                    )

            earlier: list[Exchange] = []
            query: str | None = message
            if reference.kind is ReferenceKind.CONTINUATION:
                yield Status(phase=Phase.RECALLING)
                last = await self._store.last_exchanges(
                    tenant, owner, conversation_id, limit=settings.recent_exchanges
                )
                earlier = list(reversed(last))  # the latest is what matters most
                if earlier:
                    query = f"{earlier[0].question}\n{message}"
            elif reference.kind is ReferenceKind.EARLIER:
                yield Status(phase=Phase.RECALLING)
                # What to look for is the topic. A message that gives neither a topic nor a period
                # has only itself to go by, and it is looked for by its meaning. The period was read
                # by a model, and is a hint.
                looked_for = reference.topic
                whole_message = looked_for is None and reference.when is None
                if whole_message:
                    looked_for = message[:MAX_HISTORY_QUERY_CHARS]
                recall = await self._history.search(
                    tenant,
                    owner,
                    query=looked_for,
                    when=reference.when,
                    limit=settings.history_hits,
                    strict=False,
                    by_words=not whole_message,
                )
                earlier = [hit.exchange for hit in recall.hits]
                for warning in recall.warnings:
                    yield warning
                # A message about the conversation itself ("what did I ask this morning?") has
                # nothing to look for in the documents.
                query = f"{reference.topic}\n{message}" if reference.topic else None

            if query != message:
                await _stop(searching)
                searching = (
                    None if query is None else asyncio.create_task(self._passages(owner, query))
                )
            passages: tuple[Passage, ...] = ()
            if searching is not None:
                yield Status(phase=Phase.SEARCHING)
                passages, unavailable = await searching
                if unavailable is not None:
                    yield unavailable

            prompt = self._builder.build(
                message=message,
                passages=passages,
                earlier=earlier,
                now=self._now(),
                max_input_tokens=profile.max_input_tokens,
            )
            yield Status(phase=Phase.ANSWERING)
            parts: list[str] = []
            usage: Usage | None = None
            stream = self._chat.chat_stream(
                owner,
                ChatRequest(
                    role=ModelRole.CHAT_MAIN,
                    messages=prompt.messages,
                    max_output_tokens=profile.max_output_tokens,
                ),
            )
            try:
                async for event in stream:
                    if isinstance(event, TextDelta):
                        parts.append(event.text)
                        yield Delta(text=event.text)
                    elif isinstance(event, UsageEvent):
                        usage = event.usage
                    elif isinstance(event, ErrorEvent):
                        yield Failed(code=event.code, message=event.message)
                        return
                    elif (
                        isinstance(event, FinishEvent)
                        and event.reason is FinishReason.CONTENT_FILTER
                    ):
                        yield Failed(code="content_filtered", message="the model refused to answer")
                        return
            finally:
                await stream.aclose()

            answer = "".join(parts)
            if not answer.strip():
                yield Failed(code="empty_answer", message="the model did not answer")
                return
            citations = citations_of(answer, prompt.passages)
            if citations:
                yield Cited(citations=citations)
            untrusted = bool(prompt.passages) or any(e.untrusted for e in prompt.exchanges)
            try:
                await self._store.add_exchange(
                    tenant,
                    owner,
                    conversation_id=conversation_id,
                    exchange_id=exchange_id,
                    question=message,
                    answer=answer,
                    citations=citations,
                    untrusted=untrusted,
                    now=self._now(),
                )
            except Exception:  # the answer is given; not being able to keep it is worth a line
                logger.exception("an exchange could not be saved")
            yield Finished(
                reference=reference.kind,
                exchanges_used=len(prompt.exchanges),
                passages_used=len(prompt.passages),
                untrusted_content=untrusted,
                usage=usage,
            )
        finally:
            await _stop(searching)

    async def _passages(self, owner: Owner, query: str) -> Found:
        try:
            async with asyncio.timeout(self._settings.knowledge_timeout_s):
                found = await self._documents.search(
                    owner, query, limit=self._settings.document_passages
                )
        except (FactotumError, TimeoutError) as error:
            logger.warning(
                "the documents could not be searched", extra={"reason": type(error).__name__}
            )
            return (), TurnWarning(
                code=WarningCode.DOCUMENTS_UNAVAILABLE,
                message="the documents could not be searched: the answer has none of them",
            )
        return found.passages, None


async def _stop(task: "asyncio.Task[Found] | None") -> None:
    """Give up on a search that is no longer wanted, and wait for it to be gone."""
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
