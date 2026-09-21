"""Stand-ins for what the actions service depends on (a chat model, an embedding model, a runner,
the devices that commands go to, the clock), so that the tests of everything above them need no
network and no model."""

import re
import zlib
from collections.abc import AsyncGenerator, Sequence
from datetime import UTC, datetime, timedelta
from math import sqrt
from typing import Any

from factotum.contracts.chat import (
    ChatRequest,
    ChatResult,
    EventUnion,
    FinishReason,
)
from factotum.contracts.identity import Owner
from factotum.contracts.models import ModelRole, Profile, StructuredOutputSupport
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


class ScriptedChat:
    """A chat model that answers as told and keeps what it was asked.

    ``answers`` are what it says to the next requests for a JSON answer (a dict, or an error to
    raise), in order. A request nobody scripted is a failure of the test, not a made-up answer.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[Owner, ChatRequest]] = []
        self.answers: list[dict[str, Any] | Exception] = []

    def say(self, *answers: dict[str, Any] | Exception) -> None:
        self.answers.extend(answers)

    async def chat(self, owner: Owner, request: ChatRequest) -> ChatResult:
        self.requests.append((owner, request))
        if not self.answers:
            raise AssertionError("the model was asked something that the test did not expect")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return ChatResult(text=str(answer), parsed=answer, finish_reason=FinishReason.STOP)

    def chat_stream(self, owner: Owner, request: ChatRequest) -> AsyncGenerator[EventUnion]:
        raise NotImplementedError("the actions service asks for whole answers")

    async def profile(self, owner: Owner, role: ModelRole) -> Profile:
        return Profile(
            role=role,
            max_input_tokens=8192,
            max_output_tokens=1024,
            streaming=True,
            structured_output=StructuredOutputSupport.NATIVE,
        )

    @property
    def asked(self) -> list[ChatRequest]:
        return [request for _, request in self.requests]


_WORD = re.compile(r"[a-z0-9]+")
DIMENSIONS = 64


def embed_words(text: str) -> tuple[float, ...]:
    """A vector for a text, from its words: texts that share words are near, and texts that share
    none are not. Deterministic, and nothing like a real model, which is the point: what is tested
    is what the service does with the distances."""
    vector = [0.0] * DIMENSIONS
    for word in _WORD.findall(text.casefold()):
        vector[zlib.crc32(word.encode()) % DIMENSIONS] += 1.0
    norm = sqrt(sum(x * x for x in vector))
    return tuple(x / norm for x in vector) if norm else tuple(vector)


class FakeEmbedder:
    """An embedding model that is made of words, and can be told to fail or to change."""

    def __init__(self, space_id: str = "space-a") -> None:
        self.space_id = space_id
        self.calls: list[tuple[Owner, tuple[str, ...]]] = []
        self.failure: Exception | None = None

    async def embed(self, owner: Owner, texts: Sequence[str]) -> Embedded:
        self.calls.append((owner, tuple(texts)))
        if self.failure is not None:
            raise self.failure
        return Embedded(
            vectors=tuple(embed_words(text) for text in texts),
            space_id=self.space_id,
            dimensions=DIMENSIONS,
        )
