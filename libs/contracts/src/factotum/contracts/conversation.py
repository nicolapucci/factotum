"""Conversations: what is said, what is saved of it, and what a turn answers with.

A turn carries no history (ADR 0012). Every exchange is saved, and consulted only when a message
refers to an earlier one; this is the vocabulary of that: the decision whether a message does, the
exchanges themselves, and the events a turn streams while it is answered.
"""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from factotum.contracts.base import Contract
from factotum.contracts.chat import Usage
from factotum.contracts.ids import CollectionId, ConversationId, DeviceId, DocumentId, ExchangeId

MAX_MESSAGE_CHARS = 4000
MAX_HISTORY_QUERY_CHARS = 500


class ReferenceKind(StrEnum):
    NONE = "none"
    """The message can be understood on its own."""

    CONTINUATION = "continuation"
    """It carries on the exchange just before: "and in English?", "why?", "the second one"."""

    EARLIER = "earlier"
    """It points to something said before, maybe long ago: "what you told me yesterday about the
    router"."""


class Period(StrEnum):
    """When an earlier exchange took place, as far as the message says."""

    TODAY = "today"
    YESTERDAY = "yesterday"
    THIS_WEEK = "this_week"
    LAST_WEEK = "last_week"
    THIS_MONTH = "this_month"
    LAST_MONTH = "last_month"


class Reference(Contract):
    """What is made of a message: does it refer to an earlier exchange, and to which. A small model
    reads it out of the message (see the gate of the conversation service); its words are plain, and
    a schema of it names the kinds and the periods a model may use."""

    kind: ReferenceKind
    topic: str | None = Field(default=None, max_length=200)
    """For an earlier exchange: the words to look for, in the language of the message."""

    when: Period | None = None
    """For an earlier exchange: the day, week or month the message names, if it names one."""


class TurnRequest(Contract):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    conversation_id: ConversationId | None = None
    """The conversation to carry on. Without one, the conversation of this caller and device that
    was active in the last half hour, or a new one."""


class Phase(StrEnum):
    UNDERSTANDING = "understanding"
    """Deciding whether the message refers to an earlier exchange."""

    RECALLING = "recalling"
    """Looking at earlier exchanges."""

    SEARCHING = "searching"
    """Searching the documents."""

    ANSWERING = "answering"


class WarningCode(StrEnum):
    DOCUMENTS_UNAVAILABLE = "documents_unavailable"
    """The documents could not be searched: the answer has none of them."""

    HISTORY_WORDS_ONLY = "history_words_only"
    """Earlier exchanges were searched by words only (no embedding model, or it did not answer)."""

    REFERENCE_UNAVAILABLE = "reference_unavailable"
    """It could not be told whether the message refers to an earlier exchange: none is in it."""


class Citation(Contract):
    """A passage of a document an answer cites as ``[number]``."""

    number: int = Field(ge=1)
    document_id: DocumentId
    collection_id: CollectionId
    document_title: str
    source: str | None = None
    page: int | None = Field(default=None, ge=1)
    headings: tuple[str, ...] = ()


class Started(Contract):
    type: Literal["started"] = "started"
    conversation_id: ConversationId
    exchange_id: ExchangeId


class Status(Contract):
    type: Literal["status"] = "status"
    phase: Phase


class Delta(Contract):
    model_config = ConfigDict(str_strip_whitespace=False)  # a chunk keeps the space at its edge

    type: Literal["delta"] = "delta"
    text: str


class Cited(Contract):
    type: Literal["citations"] = "citations"
    citations: tuple[Citation, ...]


class TurnWarning(Contract):
    """Something that made an answer less good than it could be, without making it fail."""

    type: Literal["warning"] = "warning"
    code: WarningCode
    message: str


class Finished(Contract):
    type: Literal["finished"] = "finished"
    reference: ReferenceKind
    exchanges_used: int = Field(ge=0)
    passages_used: int = Field(ge=0)
    untrusted_content: bool
    """The turn had somebody else's words in it: a passage of a document, or an earlier exchange
    that had. Whatever it says is data, never an instruction (see ``authorize``)."""

    usage: Usage | None = None


class Failed(Contract):
    """The turn failed after it had started; the code is the one of the matching error."""

    type: Literal["failed"] = "failed"
    code: str
    message: str


TurnEvent = Started | Status | Delta | Cited | TurnWarning | Finished | Failed

TurnEventUnion = Annotated[TurnEvent, Field(discriminator="type")]


class Exchange(Contract):
    """A question and its answer, as saved."""

    id: ExchangeId
    conversation_id: ConversationId
    seq: int = Field(ge=1)
    created_at: AwareDatetime
    question: str
    answer: str
    citations: tuple[Citation, ...] = ()
    untrusted: bool = False
    """Its turn had somebody else's words in it, so what it says may carry them."""


class Conversation(Contract):
    id: ConversationId
    started_at: AwareDatetime
    last_at: AwareDatetime
    device_id: DeviceId | None = None
    exchanges: int = Field(ge=0)


class ConversationList(Contract):
    """The conversations of a caller, the last active first."""

    conversations: tuple[Conversation, ...] = ()


class ConversationDetail(Contract):
    conversation: Conversation
    exchanges: tuple[Exchange, ...] = ()


class HistoryQuery(Contract):
    """A search of what was said before. By words and meaning when there is something to look for,
    and by the time alone when there is not ("what did I ask yesterday?")."""

    query: str | None = Field(default=None, min_length=1, max_length=MAX_HISTORY_QUERY_CHARS)
    when: Period | None = None
    limit: int = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def _has_something_to_go_by(self) -> Self:
        if self.query is None and self.when is None:
            raise ValueError("a search of the history needs words to look for, a period, or both")
        return self


class HistoryHit(Contract):
    exchange: Exchange
    score: float = Field(gt=0, le=1)
    matched_by: tuple[Literal["vector", "text"], ...] = ()
    """Empty when it was found by its time alone."""


class HistoryResult(Contract):
    hits: tuple[HistoryHit, ...] = ()
    warnings: tuple[TurnWarning, ...] = ()
