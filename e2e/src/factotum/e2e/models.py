"""The models of the house, made up: one provider of the gateway that is the chat model, the small
model that reads and plans, and the embedding model.

What a model does is a proposal that the code around it checks (ADR 0014), so this stand-in does
what a competent small model does with the few cases the tests need, in English and a little
Italian: it answers from the sources it was shown, reads a message as a question or as a command,
and gives words a direction. It proposes without looking at what it was offered, as a model that is
wrong or led astray would, so that what the tests show is that the code decides and the model does
not.
"""

import asyncio
import json
import re
import unicodedata
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
from factotum.contracts.models import BindingOptions, ModelBinding, ModelRole, ProviderKind
from factotum.contracts.testing import make_model_binding
from factotum.knowledge.testing import FakeEmbedder
from factotum.model_gateway.providers.base import (
    EmbeddingBatch,
    ResolvedBinding,
    SpeechPiece,
    role_not_supported,
)

STT_MODEL = "tiny"
VOICE = "it_IT-paola-medium"
LANGUAGES = ("it", "en")
"""What the house is spoken to in, as its speech-to-text binding says it: the questions of these
tests are in English and in Italian. The tests with real speech bind Italian only."""

# What the chat model writes when it answers a question, and so what a speaker is made to say. It
# answers in the language of the question.
CLOSING = "Let me know if there is anything else you need."
NOT_FOUND = "I did not find that in your documents."
OFFER = "Is there anything else I can look up for you?"
FROM_YOUR_DOCUMENTS = "From your documents:"

CLOSING_IT = "Fammi sapere se ti serve altro."
NOT_FOUND_IT = "Non l'ho trovato nei tuoi documenti."
OFFER_IT = "Vuoi che cerchi qualcos'altro?"
FROM_YOUR_DOCUMENTS_IT = "Dai tuoi documenti:"

ROUTER = "To reset the router, hold the reset button for ten seconds."
"""A sentence of a manual: the questions about the router are answered from it."""

FROM_THE_MANUAL = f"From your documents: {ROUTER.rstrip('.')}"
SPOKEN_ANSWER = [f"{FROM_THE_MANUAL}.", CLOSING]
"""The two sentences a speaker is made to say to a question about the router: what it says has
no references in it."""

_SOURCES = re.compile(r"=== SOURCES (\w+) ===\n(.*?)\n=== END SOURCES \1 ===", re.DOTALL)


_WORD = re.compile(r"\w{3,}")
_SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")

_ITALIAN = re.compile(
    r"\b(accendi|spegni|apri|chiudi|alza|abbassa|imposta|metti|luce|lampada"
    r"|il|lo|gli|una|uno|del|della|dei|delle|nel|nella|per|con|che|come|dove|cosa|quale|qual"
    r"|chi|perché|si|sono|mi|ti)\b|[àèéìòù]"
)


def is_italian(text: str) -> bool:
    """Whether a message is in Italian, as far as a stand-in needs to tell: it has a word that is
    Italian and not English, or an accent."""
    return _ITALIAN.search(text.casefold()) is not None


def answer_from(prompt: str) -> str:
    """What the stand-in chat model writes for a prompt: the sentence of the first source it was
    shown that has the most words of the question in it, quoted and cited, or that it found no
    source, in the language of the question. Two sentences, each long enough to be said alone."""
    question = prompt.rsplit("Message: ", 1)[-1]
    italian = is_italian(question)
    sources = _SOURCES.search(prompt)
    if sources is None:
        return f"{NOT_FOUND_IT} {OFFER_IT}" if italian else f"{NOT_FOUND} {OFFER}"
    asked = set(_WORD.findall(question.casefold()))
    _label, _, first = sources.group(2).partition("\n")
    sentences = [s.strip() for s in _SENTENCES.split(first.split("\n\n", 1)[0]) if s.strip()]
    # The most words of the question, and of those the one that says the most: a title that has
    # the same words in it says less than the sentence under it.
    nearest = max(sentences, key=lambda s: (len(asked & set(_WORD.findall(s.casefold()))), len(s)))
    if italian:
        return f"{FROM_YOUR_DOCUMENTS_IT} {nearest.rstrip('.')} [1]. {CLOSING_IT}"
    return f"{FROM_YOUR_DOCUMENTS} {nearest.rstrip('.')} [1]. {CLOSING}"


def _plain(text: str) -> str:
    """A message as a competent reader takes it: in lower case and without accents, so that a word
    that was heard with the wrong accent is still the word."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(letter for letter in decomposed if not unicodedata.combining(letter))


_NUMBER = re.compile(r"\d+")


def plan_for(shown: str) -> dict[str, Any]:
    """What the small model proposes for a message, in the shape the planner asks for. A question
    is not an action; a command names a skill, and the device if it says which."""
    message = shown.rsplit("Message: ", 1)[-1].strip()
    text = _plain(message)
    plan: dict[str, Any] = {
        "reason": "it asks for nothing to be done",
        "skill": None,
        "device": None,
        "parameters": {},
        "language": "it" if is_italian(message) else "en",
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
    elif re.search(r"\bgar", text):  # garage, and the garaf and the garrar that an ear makes of it
        action = "close" if re.search(r"\b(close|chiud)", text) else "open"
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

        self.hold_after: int | None = None
        """When set, an answer goes quiet after this many pieces and does not go on until
        ``release``: a model that has stopped in the middle of what it was saying, which is when a
        person speaks over it. (The services are put together with in-process transports, which
        hand over a response when it is whole: what a device is sent of an answer that is held is
        nothing, however many pieces were written.)"""

        self.held = asyncio.Event()
        """Set when an answer has gone quiet."""

        self.release = asyncio.Event()
        self.abandoned = 0
        """The answers that were let go of before they were written: what reads a model that has
        been stopped is not left reading it."""

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
        said = False
        try:
            # A few words at a time, as a model writes.
            for piece, start in enumerate(range(0, len(written), 16)):
                if piece == self.hold_after:
                    self.held.set()
                    await self.release.wait()
                yield TextDelta(text=written[start : start + 16])
            said = True
            yield UsageEvent(usage=Usage(input_tokens=len(prompt), output_tokens=len(written)))
            yield FinishEvent(reason=FinishReason.STOP)
        finally:
            if not said:
                self.abandoned += 1

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

    async def synthesize_stream(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, text: str
    ) -> AsyncGenerator[SpeechPiece]:
        raise role_not_supported(self.kind, "text-to-speech")
        yield  # pragma: no cover - never reached; keeps this an async generator function


def bindings(
    tenant_id: TenantId,
    *,
    speech_to_text: bool,
    text_to_speech: bool,
    whisper_model: str = STT_MODEL,
    voice: str = VOICE,
    languages: tuple[str, ...] = LANGUAGES,
) -> list[ModelBinding]:
    """What the tenant bound, the way a person writes it: the speech roles have no address. The
    model of Whisper, the voice of Piper and the languages are the ones of the stand-ins unless a
    test with real speech says where its own are."""

    def bind(
        role: ModelRole,
        provider: ProviderKind,
        model: str,
        base_url: str | None = None,
        options: BindingOptions | None = None,
    ) -> ModelBinding:
        return make_model_binding(
            tenant_id=tenant_id,
            role=role,
            provider=provider,
            model=model,
            base_url=base_url,
            options=options,
        )

    found = [
        bind(ModelRole.CHAT_MAIN, ProviderKind.OLLAMA, "the-chat-model", "http://localhost:11434"),
        bind(ModelRole.EMBEDDING, ProviderKind.OLLAMA, "the-words-model", "http://localhost:11434"),
    ]
    if speech_to_text:
        found.append(
            bind(
                ModelRole.STT,
                ProviderKind.WHISPER_LOCAL,
                whisper_model,
                options=BindingOptions(languages=languages),
            )
        )
    if text_to_speech:
        found.append(bind(ModelRole.TTS, ProviderKind.PIPER_LOCAL, voice))
    return found
