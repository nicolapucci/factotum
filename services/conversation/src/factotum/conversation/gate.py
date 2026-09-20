"""Deciding whether a message refers to something said before (ADR 0012).

A small model reads the message alone, never the conversation, and says whether it can be understood
by itself, carries on the exchange just before, or points to something said earlier (with the words
to look for and, if the message names one, the day, week or month). Because it sees no history it is
cheap, and what an earlier exchange says cannot steer it.

It is asked in the ``chat.fast`` role, which falls back to ``chat.main`` where nothing faster was
bound, and the user chooses the model, so it has to be asked in a way that any small model gets
right. That took two things (measured in ``docs/conversation.md``):

* **The examples are turns of the conversation the model is in**, a message and the answer to it,
  not a list in the instructions. A model told what the answers look like describes them; a model
  that has already given twenty of them goes on.
* **The answer says why in a few words before it says which.** A model that is asked for the label
  straight away leans one way or the other, and which way depends on the model: one calls almost
  everything a follow-up, another almost nothing. Having to say what the message names makes them
  agree.

A failure is the caller's to weigh: a turn goes on without history rather than wait.
"""

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from pydantic import Field, ValidationError

from factotum.contracts.base import Contract
from factotum.contracts.chat import ChatMessage, ChatRequest, MessageRole
from factotum.contracts.conversation import Period, Reference, ReferenceKind
from factotum.contracts.errors import GatewayError, ProviderTimeoutError
from factotum.contracts.identity import Owner
from factotum.contracts.models import ModelRole
from factotum.model_client import Chat

NONE = ReferenceKind.NONE
CONTINUATION = ReferenceKind.CONTINUATION
EARLIER = ReferenceKind.EARLIER

MAX_CHARS = 600
"""How much of a message is read. A long one is cut in the middle: what refers to the past is said
at either end, and the point of this call is to be quick."""

SYSTEM_PROMPT = """\
You look at one message that a person sent to their assistant. You are not shown the conversation. \
Decide whether the message can be understood by itself. Answer with JSON only.

"reason" is a few words on why.

"kind" is one of:
- "none": it can. It says what it is about: a question, a request, a command, a greeting, thanks.
- "continuation": it cannot: it leaves out what it is about and only makes sense as a reply to \
the exchange that has just taken place. It says "it", "that", "the other one", "the second", or \
asks to repeat, to change, to go on, to explain again, or is only "why?" or "how much?".
- "earlier": it cannot, because it points back to something the two of them said in an earlier \
exchange, maybe long ago: "you told me", "we talked about", "I asked you", "the recipe you gave me".

Only for "earlier": "topic" is what the message is about, in a few words taken from the message \
itself, not translated, left out when it does not say; "when" is the day, week or month the \
message names: "today", "yesterday", "this_week", "last_week", "this_month" or "last_month", \
left out when it names none.\
"""


class Reading(Contract):
    """What the model is asked to answer: why, in a few words, and then what the message is."""

    reason: str = Field(max_length=120)
    kind: ReferenceKind
    topic: str | None = Field(default=None, max_length=200)
    when: Period | None = None

    def reference(self) -> Reference:
        """The decision: the topic and the period mean something only for a message that points to
        something said earlier."""
        if self.kind is not EARLIER:
            return Reference(kind=self.kind)
        return Reference(kind=self.kind, topic=self.topic or None, when=self.when)


RESPONSE_SCHEMA: dict[str, Any] = Reading.model_json_schema()
"""What a model is asked to answer with: the shape of ``Reading``."""


@dataclass(frozen=True, slots=True)
class Example:
    """A message and how it is read, which the model is shown as if it had answered it itself."""

    message: str
    reason: str
    reference: Reference

    def answer(self) -> str:
        said: dict[str, str] = {"reason": self.reason, "kind": self.reference.kind.value}
        if self.reference.topic is not None:
            said["topic"] = self.reference.topic
        if self.reference.when is not None:
            said["when"] = self.reference.when.value
        return json.dumps(said, ensure_ascii=False)


def _earlier(topic: str | None = None, when: Period | None = None) -> Reference:
    return Reference(kind=EARLIER, topic=topic, when=when)


EXAMPLES: tuple[Example, ...] = (
    Example("How tall is Mount Everest?", "asks the height of a mountain", Reference(kind=NONE)),
    Example(
        "Can you say it more politely?",
        "asks to change 'it' without saying what",
        Reference(kind=CONTINUATION),
    ),
    Example(
        "What was that song you recommended on Tuesday?",
        "asks about a song the assistant recommended",
        _earlier("song"),
    ),
    Example("Dimmi come si prepara il caffè", "asks how to make coffee", Reference(kind=NONE)),
    Example("Come mai?", "asks why without saying about what", Reference(kind=CONTINUATION)),
    Example("Play some jazz in the living room", "asks for music in a room", Reference(kind=NONE)),
    Example(
        "Il consiglio sul mal di schiena che mi hai dato ieri",
        "refers to advice the assistant gave yesterday",
        _earlier("mal di schiena", Period.YESTERDAY),
    ),
    Example("Go on", "asks to go on with what was being said", Reference(kind=CONTINUATION)),
    Example("Buonasera", "is a greeting", Reference(kind=NONE)),
    Example(
        "And for children?",
        "asks 'and for children' as a follow-up",
        Reference(kind=CONTINUATION),
    ),
    Example(
        "What should I pack for a week in Norway?",
        "asks what to pack for a trip",
        Reference(kind=NONE),
    ),
    Example(
        "Last month you explained how compound interest works",
        "refers to an explanation given last month",
        _earlier("compound interest", Period.LAST_MONTH),
    ),
    Example(
        "In che senso?",
        "asks what was meant without saying what",
        Reference(kind=CONTINUATION),
    ),
    Example(
        "Quali sono i sintomi del raffreddore?",
        "asks about the symptoms of a cold",
        Reference(kind=NONE),
    ),
    Example(
        "Non ho capito, puoi rispiegarlo?",
        "asks to explain 'it' again",
        Reference(kind=CONTINUATION),
    ),
    Example(
        "Cosa avevamo detto sul viaggio a Lisbona?",
        "asks what they said about a trip",
        _earlier("viaggio Lisbona"),
    ),
    Example("Close the garage door", "commands to close a door", Reference(kind=NONE)),
    Example(
        "Ok, e dopo?",
        "asks what comes after without saying what",
        Reference(kind=CONTINUATION),
    ),
    Example(
        "What does the contract say about cancellation?",
        "asks about a clause of a document",
        Reference(kind=NONE),
    ),
    Example(
        "The joke about penguins you told me this morning",
        "refers to a joke told this morning",
        _earlier("penguins", Period.TODAY),
    ),
    Example(
        "Give me more details",
        "asks for details of something not named",
        Reference(kind=CONTINUATION),
    ),
    Example("Grazie, sei stato utile", "says thanks", Reference(kind=NONE)),
    Example(
        "Di cosa abbiamo parlato lunedì?",
        "asks what they talked about on a day",
        _earlier(),
    ),
    Example("Where is the nearest pharmacy?", "asks where a pharmacy is", Reference(kind=NONE)),
    Example(
        "Spiegami come funziona la fotosintesi",
        "asks to explain photosynthesis",
        Reference(kind=NONE),
    ),
)


def _shown() -> tuple[ChatMessage, ...]:
    turns: list[ChatMessage] = []
    for example in EXAMPLES:
        turns.append(ChatMessage(role=MessageRole.USER, content=example.message))
        turns.append(ChatMessage(role=MessageRole.ASSISTANT, content=example.answer()))
    return tuple(turns)


_SHOWN = _shown()


def shortened(message: str) -> str:
    """What of a message the model reads: all of it, or its two ends."""
    if len(message) <= MAX_CHARS:
        return message
    half = MAX_CHARS // 2
    return f"{message[:half].rstrip()} … {message[-half:].lstrip()}"


class ReferenceGate:
    def __init__(self, chat: Chat, *, timeout_s: float = 8.0) -> None:
        self._chat = chat
        self._timeout_s = timeout_s

    async def decide(self, owner: Owner, message: str) -> Reference:
        """Whether ``message`` refers to an earlier exchange. Raises a ``FactotumError`` when it
        cannot be told: the gateway did not answer, the model did not answer in time or with
        something that is not a decision."""
        request = ChatRequest(
            role=ModelRole.CHAT_FAST,
            messages=(
                ChatMessage(role=MessageRole.SYSTEM, content=SYSTEM_PROMPT),
                *_SHOWN,
                ChatMessage(role=MessageRole.USER, content=shortened(message)),
            ),
            response_schema=RESPONSE_SCHEMA,
            temperature=0,
            max_output_tokens=160,
        )
        try:
            async with asyncio.timeout(self._timeout_s):
                result = await self._chat.chat(owner, request)
        except TimeoutError:
            raise ProviderTimeoutError("the model did not decide in time") from None
        try:
            return Reading.model_validate(result.parsed).reference()
        except ValidationError:
            raise GatewayError("the model's answer is not a decision about the message") from None
