"""The models of the house, made up: one provider of the gateway that is the chat model, the small
model that reads and plans, and the embedding model.

What a model does is a proposal that the code around it checks (ADR 0014), so this stand-in does
what a competent small model does with the few cases the tests need, in English and a little
Italian: it answers from the sources it was shown, reads a message as a question or as a command,
and gives words a direction. It proposes without looking at what it was offered, as a model that is
wrong or led astray would, so that what the tests show is that the code decides and the model does
not.
"""

import json
import re
from collections.abc import AsyncGenerator, Sequence
from typing import Any

import httpx

from factotum.contracts.audio import AudioPayload
from factotum.contracts.chat import (
    ChatRequest,
    ChatResult,
    EventUnion,
    FinishEvent,
    FinishReason,
    TextDelta,
    Usage,
    UsageEvent,
)
from factotum.contracts.ids import TenantId
from factotum.contracts.models import ModelBinding, ModelRole, ProviderKind
from factotum.contracts.testing import make_model_binding
from factotum.knowledge.testing import FakeEmbedder
from factotum.model_gateway.providers.base import (
    EmbeddingBatch,
    ResolvedBinding,
    role_not_supported,
)

STT_MODEL = "tiny"
VOICE = "it_IT-paola-medium"

# What the chat model writes when it answers a question, and so what a speaker is made to say.
CLOSING = "Let me know if there is anything else you need."
NOT_FOUND = "I did not find that in your documents."
OFFER = "Is there anything else I can look up for you?"

ROUTER = "To reset the router, hold the reset button for ten seconds."
"""A sentence of a manual: the questions about the router are answered from it."""

FROM_THE_MANUAL = f"From your documents: {ROUTER.rstrip('.')}"
SPOKEN_ANSWER = [f"{FROM_THE_MANUAL}.", CLOSING]
"""The two sentences a speaker is made to say to a question about the router: what it says has
no references in it."""

_SOURCES = re.compile(r"=== SOURCES (\w+) ===\n(.*?)\n=== END SOURCES \1 ===", re.DOTALL)


_WORD = re.compile(r"\w{3,}")
_SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")


def answer_from(prompt: str) -> str:
    """What the stand-in chat model writes for a prompt: the sentence of the first source it was
    shown that has the most words of the question in it, quoted and cited, or that it found no
    source. Two sentences, each long enough to be said alone."""
    sources = _SOURCES.search(prompt)
    if sources is None:
        return f"{NOT_FOUND} {OFFER}"
    asked = set(_WORD.findall(prompt.rsplit("Message: ", 1)[-1].casefold()))
    _label, _, first = sources.group(2).partition("\n")
    sentences = [s.strip() for s in _SENTENCES.split(first.split("\n\n", 1)[0]) if s.strip()]
    # The most words of the question, and of those the one that says the most: a title that has
    # the same words in it says less than the sentence under it.
    nearest = max(sentences, key=lambda s: (len(asked & set(_WORD.findall(s.casefold()))), len(s)))
    return f"From your documents: {nearest.rstrip('.')} [1]. {CLOSING}"


_ITALIAN = re.compile(r"\b(accendi|spegni|apri|chiudi|alza|abbassa|luce|lampada)\b")
_NUMBER = re.compile(r"\d+")


def plan_for(shown: str) -> dict[str, Any]:
    """What the small model proposes for a message, in the shape the planner asks for. A question
    is not an action; a command names a skill, and the device if it says which."""
    message = shown.rsplit("Message: ", 1)[-1].strip()
    text = message.casefold()
    plan: dict[str, Any] = {
        "reason": "it asks for nothing to be done",
        "skill": None,
        "device": None,
        "parameters": {},
        "language": "it" if _ITALIAN.search(text) else "en",
        "question": None,
        "ask": None,
    }
    if message.endswith("?"):
        return plan
    plan["reason"] = "it asks for something to be done"
    if "volume" in text:
        level = _NUMBER.search(text)
        plan["skill"] = "volume"
        plan["parameters"] = {"level": int(level.group())} if level else {}
    elif re.search(r"\b(lamp|light|luce|lampada)\b", text):
        state = "off" if re.search(r"\b(off|spegni)\b", text) else "on"
        own = re.search(r"\b(my|bedroom|mia|camera)\b", text)
        plan["skill"] = "lamp"
        plan["device"] = "Bedroom lamp" if own else "Living room lamp"
        plan["parameters"] = {"state": state}
    elif "garage" in text:
        action = "close" if re.search(r"\b(close|chiudi)\b", text) else "open"
        plan["skill"] = "garage-door"
        plan["device"] = "Garage door"
        plan["parameters"] = {"action": action}
        if plan["language"] == "it":
            plan["question"] = (
                f"Vuoi davvero che {'chiuda' if action == 'close' else 'apra'} il garage?"
            )
        else:
            plan["question"] = f"Do you really want me to {action} the garage door?"
    else:
        plan["reason"] = "no skill fits"
    return plan


class HouseholdModel:
    """The chat model and the embedding model of the house, as one provider of the gateway."""

    kind = ProviderKind.OLLAMA

    def __init__(self) -> None:
        self.prompts: list[str] = []
        """What each answer was asked, as the model saw it: the sources, and the message."""

        self.readings = 0
        """How many times a message was read for a reference to the past."""

        self.plans: list[str] = []
        """What the planner showed the model, one for each message that got as far as it."""

        self.confirmations: list[str] = []
        """What the reader of a yes or a no showed the model."""

        self._words = FakeEmbedder(dimensions=64)

    def native_structured_output(self, resolved: ResolvedBinding) -> bool:
        return True

    async def chat(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, request: ChatRequest
    ) -> ChatResult:
        """What is asked whole is always something with a shape, and the shape says what it is."""
        asked = (request.response_schema or {}).get("properties", {})
        shown = request.messages[-1].content
        answer: dict[str, Any]
        if "skill" in asked:  # which skill a message asks for
            self.plans.append(shown)
            answer = plan_for(shown)
        elif "answer" in asked:  # whether what was said is a yes
            self.confirmations.append(shown)
            answer = {"reason": "it is not a plain yes or no", "answer": "other"}
        elif "kind" in asked:  # whether a message refers to the past
            self.readings += 1
            answer = {"reason": "it stands alone", "kind": "none"}
        else:
            raise AssertionError(f"the stand-in does not know what it is asked: {sorted(asked)}")
        return ChatResult(text=json.dumps(answer), finish_reason=FinishReason.STOP)

    async def chat_stream(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, request: ChatRequest
    ) -> AsyncGenerator[EventUnion]:
        prompt = request.messages[-1].content
        self.prompts.append(prompt)
        written = answer_from(prompt)
        for start in range(0, len(written), 16):  # a few words at a time, as a model writes
            yield TextDelta(text=written[start : start + 16])
        yield UsageEvent(usage=Usage(input_tokens=len(prompt), output_tokens=len(written)))
        yield FinishEvent(reason=FinishReason.STOP)

    async def embed(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, inputs: Sequence[str]
    ) -> EmbeddingBatch:
        return EmbeddingBatch(vectors=tuple(self._words.vector(text) for text in inputs))

    async def transcribe(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, audio: AudioPayload
    ) -> str:
        raise role_not_supported(self.kind, "speech-to-text")

    async def synthesize(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, text: str
    ) -> AudioPayload:
        raise role_not_supported(self.kind, "text-to-speech")


def bindings(
    tenant_id: TenantId, *, speech_to_text: bool, text_to_speech: bool
) -> list[ModelBinding]:
    """What the tenant bound, the way a person writes it: the speech roles have no address."""

    def bind(
        role: ModelRole, provider: ProviderKind, model: str, base_url: str | None = None
    ) -> ModelBinding:
        return make_model_binding(
            tenant_id=tenant_id, role=role, provider=provider, model=model, base_url=base_url
        )

    found = [
        bind(ModelRole.CHAT_MAIN, ProviderKind.OLLAMA, "the-chat-model", "http://localhost:11434"),
        bind(ModelRole.EMBEDDING, ProviderKind.OLLAMA, "the-words-model", "http://localhost:11434"),
    ]
    if speech_to_text:
        found.append(bind(ModelRole.STT, ProviderKind.WHISPER_LOCAL, STT_MODEL))
    if text_to_speech:
        found.append(bind(ModelRole.TTS, ProviderKind.PIPER_LOCAL, VOICE))
    return found
