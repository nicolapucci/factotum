"""Reading a yes or a no.

When the assistant has asked a plain yes or no before it does something, the next thing the person
says on that device is the answer, or is not: it may be something else altogether. Which of the
three it is has to be read in any language, and it decides whether something that has an effect
happens, so it is read by the words where the words leave no doubt and by a small model where they
do, and **whatever is not clearly a yes is not one**: a model that cannot be reached, a message that
is long, an answer that adds something ("yes, and also...") are *other*, and other means that the
question is dropped and the message is taken as a new one. A yes authenticates nobody (ADR 0004): it
protects against a misunderstanding and against words that are not the person's own.
"""

import asyncio
import json
import logging
import re
import unicodedata
from enum import StrEnum

from pydantic import Field, ValidationError

from factotum.contracts.base import Contract
from factotum.contracts.chat import ChatMessage, ChatRequest, MessageRole
from factotum.contracts.errors import FactotumError
from factotum.contracts.identity import Owner
from factotum.contracts.models import ModelRole
from factotum.model_client import Chat

logger = logging.getLogger("factotum.actions")

MAX_ANSWER_CHARS = 200
"""A yes or a no is short: what is longer is something else and is not read by a model at all."""


class Answer(StrEnum):
    YES = "yes"
    NO = "no"
    OTHER = "other"


def normalize(text: str) -> str:
    """Words only, in lower case, without accents or punctuation: what is compared."""
    plain = unicodedata.normalize("NFKD", text.casefold())
    without_accents = "".join(c for c in plain if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", without_accents).split())


_YES = frozenset(
    normalize(phrase)
    for phrase in [
        "yes",
        "yeah",
        "yep",
        "yup",
        "sure",
        "ok",
        "okay",
        "of course",
        "go ahead",
        "do it",
        "please do",
        "yes please",
        "yes do it",
        "yes go ahead",
        "sure go ahead",
        "confirm",
        "confirmed",
        "correct",
        "affirmative",
        "si",
        "sì",
        "certo",
        "certamente",
        "va bene",
        "fai pure",
        "fallo",
        "procedi",
        "conferma",
        "confermo",
        "esatto",
        "assolutamente si",
        "si grazie",
        "si per favore",
        "si certo",
        "si fallo",
        "si fai pure",
        "ok fallo",
        "oui",
        "claro",
        "ja",
    ]
)
_NO = frozenset(
    normalize(phrase)
    for phrase in [
        "no",
        "nope",
        "nah",
        "don't",
        "do not",
        "dont",
        "cancel",
        "stop",
        "never mind",
        "nevermind",
        "forget it",
        "no thanks",
        "no thank you",
        "abort",
        "not now",
        "don't do it",
        "do not do it",
        "no grazie",
        "annulla",
        "lascia stare",
        "lascia perdere",
        "non farlo",
        "non ora",
        "fermo",
        "fermati",
        "ferma",
        "niente",
        "non fare niente",
        "nein",
    ]
)


def read_by_words(message: str) -> Answer | None:
    """A yes or a no when the words say nothing else; nothing when they leave a doubt."""
    text = normalize(message)
    if text in _YES:
        return Answer.YES
    if text in _NO:
        return Answer.NO
    return None


class Reading(Contract):
    """What the model is asked to answer: why, in a few words, and then which of the three."""

    reason: str = Field(max_length=120)
    answer: Answer


RESPONSE_SCHEMA = Reading.model_json_schema()

SYSTEM_PROMPT = """\
An assistant asked a person a yes or no question before doing something. You are shown the \
question and what the person answered. Decide whether the answer is a clear yes to exactly what \
was asked, a clear no, or something else. Answer with JSON only.

"reason" is a few words on why.

"answer" is one of:
- "yes": the person clearly agrees to what was asked, and to nothing more or different.
- "no": the person clearly refuses, or asks not to do it.
- "other": anything else: a different request, a question, a change to what was asked, a doubt, \
a request to wait, something unrelated, or a yes that adds something.\
"""

_EXAMPLES: tuple[tuple[str, str, str, Answer], ...] = (
    ("Do you want me to open the garage door?", "sure, go ahead", "agrees", Answer.YES),
    ("Do you want me to open the garage door?", "no wait, not yet", "asks to wait", Answer.NO),
    (
        "Do you want me to open the garage door?",
        "what's the weather like?",
        "asks something unrelated",
        Answer.OTHER,
    ),
    (
        "Vuoi davvero che apra il garage?",
        "sì, ma solo a metà",
        "agrees but changes what was asked",
        Answer.OTHER,
    ),
    ("Vuoi davvero che apra il garage?", "certo, apri pure", "agrees", Answer.YES),
    ("Vuoi davvero che apra il garage?", "meglio di no", "refuses", Answer.NO),
    (
        "Do you want me to restart the router?",
        "yes and turn on the lamp too",
        "agrees and asks for more",
        Answer.OTHER,
    ),
    ("Do you want me to restart the router?", "why?", "asks a question", Answer.OTHER),
    ("Do you want me to restart the router?", "yes please", "agrees", Answer.YES),
    ("Vuoi che spenga la luce?", "mmm non so", "is not sure", Answer.OTHER),
)


def _shown() -> tuple[ChatMessage, ...]:
    turns: list[ChatMessage] = []
    for question, answer, reason, kind in _EXAMPLES:
        turns.append(ChatMessage(role=MessageRole.USER, content=_ask(question, answer)))
        turns.append(
            ChatMessage(
                role=MessageRole.ASSISTANT,
                content=json.dumps({"reason": reason, "answer": kind.value}, ensure_ascii=False),
            )
        )
    return tuple(turns)


def _ask(question: str, answer: str) -> str:
    return f"Question: {question}\nAnswer: {answer}"


_SHOWN = _shown()


class ConfirmationReader:
    def __init__(self, chat: Chat, *, timeout_s: float = 6.0) -> None:
        self._chat = chat
        self._timeout_s = timeout_s

    async def read(self, owner: Owner, question: str, message: str) -> Answer:
        """Whether ``message`` is a yes, a no or something else, as an answer to ``question``.
        Never raises: what cannot be read is *other*."""
        said = message.strip()
        if len(said) > MAX_ANSWER_CHARS:
            return Answer.OTHER
        by_words = read_by_words(said)
        if by_words is not None:
            return by_words
        request = ChatRequest(
            role=ModelRole.CHAT_FAST,
            messages=(
                ChatMessage(role=MessageRole.SYSTEM, content=SYSTEM_PROMPT),
                *_SHOWN,
                ChatMessage(role=MessageRole.USER, content=_ask(question[:300], said)),
            ),
            response_schema=RESPONSE_SCHEMA,
            temperature=0,
            max_output_tokens=80,
        )
        try:
            async with asyncio.timeout(self._timeout_s):
                result = await self._chat.chat(owner, request)
            return Reading.model_validate(result.parsed).answer
        except (FactotumError, TimeoutError, ValidationError) as error:
            logger.warning("an answer could not be read", extra={"reason": type(error).__name__})
            return Answer.OTHER
