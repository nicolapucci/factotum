"""Stand-ins for what a turn depends on (a chat model, the documents, an embedding model, the
clock), so that the tests of everything above them need no network and no model."""

import re
import zlib
from collections.abc import AsyncGenerator, Sequence
from datetime import UTC, datetime, timedelta
from math import sqrt
from uuid import uuid4

from factotum.contracts.chat import (
    ChatRequest,
    ChatResult,
    ErrorEvent,
    EventUnion,
    FinishEvent,
    FinishReason,
    TextDelta,
    Usage,
    UsageEvent,
)
from factotum.contracts.conversation import Reference, ReferenceKind, TurnEvent, TurnRequest
from factotum.contracts.identity import Owner, Principal
from factotum.contracts.ids import ChunkId, CollectionId, ConversationId, DocumentId
from factotum.contracts.knowledge import Location, Passage, SearchResult, SearchWarning
from factotum.contracts.models import ModelRole, Profile, StructuredOutputSupport
from factotum.conversation.context import PromptBuilder
from factotum.conversation.gate import ReferenceGate
from factotum.conversation.history import History
from factotum.conversation.store import InMemoryStore
from factotum.conversation.turn import TurnEngine, TurnSettings
from factotum.model_client import Embedded

START = datetime(2026, 9, 21, 9, 30, tzinfo=UTC)


class Clock:
    """A clock that moves when it is told to."""

    def __init__(self, now: datetime = START) -> None:
        self.current = now

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **how_much: float) -> datetime:
        self.current += timedelta(**how_much)
        return self.current


class FakeChat:
    """A chat model that answers as told and keeps what it was asked.

    ``decisions`` are what the model says about the next messages it is asked to classify (a
    ``Reference``, or an error to raise), in order; without one it says the message stands alone.
    ``answers`` are the next streamed answers, each a list of what comes out: text for a delta, an
    exception to raise when it is reached, an ``ErrorEvent`` to send.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[Owner, ChatRequest]] = []
        self.decisions: list[Reference | Exception] = []
        self.answers: list[Sequence[str | Exception | ErrorEvent]] = []
        self.usage: Usage | None = Usage(input_tokens=100, output_tokens=20)
        self.profile_calls = 0
        self.streams_open = 0
        """Streams begun and not yet closed: a turn that ends must leave none."""

        self.max_input_tokens = 8192
        self.max_output_tokens: int | None = 1024
        self.profile_failure: Exception | None = None

    @property
    def classifications(self) -> list[ChatRequest]:
        return [r for _, r in self.requests if r.response_schema is not None]

    @property
    def streams(self) -> list[ChatRequest]:
        return [r for _, r in self.requests if r.response_schema is None]

    async def chat(self, owner: Owner, request: ChatRequest) -> ChatResult:
        self.requests.append((owner, request))
        decision = self.decisions.pop(0) if self.decisions else Reference(kind=ReferenceKind.NONE)
        if isinstance(decision, Exception):
            raise decision
        return ChatResult(
            text=decision.model_dump_json(),
            parsed=decision.model_dump(mode="json", exclude_none=True),
            finish_reason=FinishReason.STOP,
        )

    async def chat_stream(self, owner: Owner, request: ChatRequest) -> AsyncGenerator[EventUnion]:
        self.requests.append((owner, request))
        script = self.answers.pop(0) if self.answers else ["An answer."]
        self.streams_open += 1
        try:
            for item in script:
                if isinstance(item, Exception):
                    raise item
                if isinstance(item, ErrorEvent):
                    yield item
                    return
                yield TextDelta(text=item)
            if self.usage is not None:
                yield UsageEvent(usage=self.usage)
            yield FinishEvent(reason=FinishReason.STOP)
        finally:
            self.streams_open -= 1

    async def profile(self, owner: Owner, role: ModelRole) -> Profile:
        self.profile_calls += 1
        if self.profile_failure is not None:
            raise self.profile_failure
        return Profile(
            role=role,
            max_input_tokens=self.max_input_tokens,
            max_output_tokens=self.max_output_tokens,
            streaming=True,
            structured_output=StructuredOutputSupport.NATIVE,
        )


def passage(
    text: str = "Hold the reset button for ten seconds.",
    *,
    title: str = "Router manual",
    context: str | None = None,
    page: int | None = 12,
    headings: tuple[str, ...] = ("Router", "Reset"),
    score: float = 1.0,
) -> Passage:
    return Passage(
        chunk_id=ChunkId(uuid4()),
        collection_id=CollectionId(uuid4()),
        document_id=DocumentId(uuid4()),
        document_title=title,
        text=text,
        context=context or text,
        location=Location(page=page, headings=headings),
        score=score,
        matched_by=("vector", "text"),
    )


class FakeDocuments:
    """The search of the user's documents, with what it finds and how it fails told by the test."""

    def __init__(self, *passages: Passage) -> None:
        self.passages = list(passages)
        self.warnings: list[SearchWarning] = []
        self.queries: list[tuple[Owner, str, int]] = []
        self.failure: Exception | None = None

    async def search(self, owner: Owner, query: str, *, limit: int) -> SearchResult:
        self.queries.append((owner, query, limit))
        if self.failure is not None:
            raise self.failure
        return SearchResult(passages=tuple(self.passages[:limit]), warnings=tuple(self.warnings))


_FILLER = frozenset(
    [
        "a",
        "an",
        "the",
        "is",
        "in",
        "of",
        "to",
        "and",
        "or",
        "do",
        "i",
        "how",
        "what",
        "did",
        "you",
        "me",
        "it",
        "that",
        "this",
        "answer",
    ]
)
"""Words that make no direction, as a real model gives them little weight ("answer" is what the fake
chat answers with, so that every exchange has it)."""


class WordEmbedder:
    """An embedding model in which a word makes a direction, so that texts sharing words are near.
    It can be told to fail, and it keeps what it was asked."""

    def __init__(self, *, space_id: str = "emb-words", dimensions: int = 256) -> None:
        self.space_id = space_id
        self.dimensions = dimensions
        self.calls: list[tuple[Owner, list[str]]] = []
        self.failures: list[Exception | None] = []
        """One for each call, in turn: an error is raised, ``None`` lets the call through."""

    async def embed(self, owner: Owner, texts: Sequence[str]) -> Embedded:
        self.calls.append((owner, list(texts)))
        if self.failures:
            failure = self.failures.pop(0)
            if failure is not None:
                raise failure
        return Embedded(tuple(self.vector(t) for t in texts), self.space_id, self.dimensions)

    def vector(self, text: str) -> tuple[float, ...]:
        counts = [0.0] * self.dimensions
        for word in re.findall(r"\w+", text.casefold()):
            if word in _FILLER:
                continue
            counts[zlib.crc32(word.encode()) % self.dimensions] += 1.0
        norm = sqrt(sum(c * c for c in counts)) or 1.0
        return tuple(c / norm for c in counts)


class Harness:
    """A turn engine put together from the stand-ins, and what is needed to look at what it did."""

    def __init__(self, *, settings: TurnSettings | None = None) -> None:
        self.store = InMemoryStore()
        self.clock = Clock()
        self.chat = FakeChat()
        self.documents = FakeDocuments()
        self.embedder = WordEmbedder()
        self.history = History(self.store, self.embedder, now=self.clock, min_similarity=0.3)
        self.engine = TurnEngine(
            self.store,
            self.chat,
            self.documents,
            self.history,
            ReferenceGate(self.chat),
            PromptBuilder(nonce=lambda: "nonce123"),
            settings=settings,
            now=self.clock,
        )

    async def turn(
        self,
        principal: Principal,
        message: str,
        *,
        conversation_id: ConversationId | None = None,
    ) -> list[TurnEvent]:
        """Everything a turn streamed."""
        request = TurnRequest(message=message, conversation_id=conversation_id)
        return [event async for event in self.engine.run(principal, request)]
