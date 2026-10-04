"""The assistant end to end: somebody talks to a device and the answer comes out of its speaker.

Everything between the two is the real code, put together in one process the way the compose file
puts the services together: the terminal client on one side of a real WebSocket, the edge on the
other, and behind the edge the conversation, knowledge and model-gateway applications, each reached
through its own HTTP interface (an in-memory transport, so no port is needed) and each with its
store in memory instead of Postgres. Nothing above the stores is replaced.

What cannot run in a unit test is made up:

* **The hardware of the device.** A recording is a WAV file that carries the words that were said
  in it (``recording_of``), and a speaker only remembers what it was asked to play. The wake word is
  a ``FakeVoiceInput`` that hands the terminal one recording after another. This is the mock of the
  client. A client that streams audio and can be interrupted (ADR 0021) will need one of its own.
* **The models.** Whisper and Piper are the real adapters of the gateway on top of stand-in engines:
  the first reads the words out of a recording, the second writes a clip that carries the words it
  was asked to say, so that a test can tell what a speaker was made to say. The chat model writes
  an answer out of the sources it was shown, and the embedding model gives words a direction.

The tests of one layer at a time are with each service. These are the ones that need all of them.
"""

import asyncio
import io
import json
import re
import struct
import wave
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

import httpx
import uvicorn
from fastapi import FastAPI

from factotum.common.devauth import SUBJECT_HEADER, DevAuthenticator, DevPrincipalDelegation
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
from factotum.contracts.conversation import ConversationDetail, ConversationList, Exchange
from factotum.contracts.devices import SystemDeviceRecord
from factotum.contracts.edge import MAX_FRAME_BYTES, CloseCode, TurnEnded, TurnStatus
from factotum.contracts.knowledge import (
    CollectionCreate,
    CollectionOwner,
    Document,
    DocumentStatus,
    DocumentText,
)
from factotum.contracts.models import ModelBinding, ModelRole, ProviderKind
from factotum.contracts.testing import make_model_binding
from factotum.conversation.api import create_app as create_conversation_app
from factotum.conversation.context import PromptBuilder
from factotum.conversation.documents import KnowledgeClient
from factotum.conversation.gate import ReferenceGate
from factotum.conversation.history import History
from factotum.conversation.service import ConversationService
from factotum.conversation.store import InMemoryStore as ConversationMemory
from factotum.conversation.turn import TurnEngine
from factotum.edge.api import create_app as create_edge_app
from factotum.edge.conversation import ConversationClient
from factotum.edge.registry import FileDeviceRegistry
from factotum.edge.router import Sessions
from factotum.edge.testing import TENANT, TEST, USER, ReportingServer, make_device, quick_settings
from factotum.knowledge.api import create_app as create_knowledge_app
from factotum.knowledge.ingestion import Ingestor
from factotum.knowledge.service import KnowledgeService
from factotum.knowledge.store import InMemoryStore as KnowledgeMemory
from factotum.knowledge.store import KnowledgeStore
from factotum.knowledge.testing import FakeEmbedder, parse_in_process
from factotum.model_client import DevDelegation, GatewayChat, GatewayEmbedder, GatewaySpeech
from factotum.model_gateway.api import create_app as create_gateway_app
from factotum.model_gateway.bindings import InMemoryBindingRepository
from factotum.model_gateway.core import ModelGateway
from factotum.model_gateway.providers.base import (
    EmbeddingBatch,
    ResolvedBinding,
    role_not_supported,
)
from factotum.model_gateway.providers.piper_local import PiperLocalAdapter
from factotum.model_gateway.providers.whisper_local import WhisperLocalAdapter
from factotum.model_gateway.retry import RetryPolicy
from factotum.model_gateway.secrets import EnvSecretResolver
from factotum.model_gateway.testing import Sleeps, TrackedClients
from factotum.terminal.cli import converse_by_voice
from factotum.terminal.client import DeviceClient
from factotum.terminal.connection import WebSocketConnection
from factotum.terminal.device import TerminalDevice
from factotum.terminal.profiles import get_profile
from factotum.terminal.testing import FakeVoiceInput, FakeVoiceOutput, RecordingRenderer

TIMEOUT_S = 10.0
"""What a test waits for something that should be immediate: a stuck one ends instead of hanging."""

STT_MODEL = "tiny"
VOICE = "it_IT-paola-medium"

ROUTER = "To reset the router, hold the reset button for ten seconds."
ROUTER_IN_ITALIAN = "Il router è nello studio, accanto alla libreria, perché lì arriva il cavo."
SAFE = "The code of the safe is 4711."
QUESTION = "How do I reset the router?"
SAFE_QUESTION = "What is the code of the safe?"

# What the stand-in chat model writes, and so what a speaker is made to say.
FROM_THE_MANUAL = "From your documents: To reset the router, hold the reset button for ten seconds"
CLOSING = "Let me know if there is anything else you need."
SPOKEN_ANSWER = [f"{FROM_THE_MANUAL}.", CLOSING]
"""The two sentences a speaker is made to say to a question about the router: what it says has
no references in it."""

NOT_FOUND = "I did not find that in your documents."
OFFER = "Is there anything else I can look up for you?"


# ---- a voice, made up ------------------------------------------------------------------------


def wav_saying(words: str, rate: int, *, seconds: float = 0.0) -> bytes:
    """A WAV file whose samples are the bytes of ``words``, and silence after them (a little, or
    enough for it to last ``seconds``): a voice that can be read back. The stand-in of Whisper reads
    the words out of a recording, and a test reads what the stand-in of Piper was made to say."""
    spoken = words.encode()
    silence = max(160, round(rate * seconds) - len(spoken))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(rate)
        file.writeframes(struct.pack(f"<{len(spoken)}h", *spoken) + bytes(2 * silence))
    return buffer.getvalue()


def words_in(wav: bytes) -> str:
    with wave.open(io.BytesIO(wav)) as file:
        frames = file.readframes(file.getnframes())
    samples = struct.unpack(f"<{len(frames) // 2}h", frames)
    return bytes(samples).rstrip(b"\x00").decode()


def recording_of(words: str, *, seconds: float = 0.0) -> AudioPayload:
    """What a microphone hands over after somebody said ``words``; no words is a silence."""
    return AudioPayload(sample_rate=16_000, data=wav_saying(words, 16_000, seconds=seconds))


def said_in(clip: AudioPayload) -> str:
    return words_in(clip.data)


class StandInWhisper:
    """The engine of ``whisper_local``: it reads the words out of a recording made by
    ``recording_of``."""

    def __init__(self) -> None:
        self.models: list[str] = []
        """The model sizes the adapter asked for: what a binding said reaches the engine."""

        self.heard: list[str] = []
        self.broken = False

    def build(self, model: str) -> "StandInWhisper":
        self.models.append(model)
        return self

    def transcribe_wav(self, wav_bytes: bytes) -> str:
        if self.broken:
            raise RuntimeError("the model fell over")
        self.heard.append(words_in(wav_bytes))
        return self.heard[-1]


class StandInPiper:
    """The engine of ``piper_local``: it writes a clip that carries the words it was asked for."""

    def __init__(self) -> None:
        self.voices: list[str] = []
        self.spoken: list[str] = []

    def build(self, voice: str) -> "StandInPiper":
        self.voices.append(voice)
        return self

    def synthesize_wav(self, text: str) -> bytes:
        self.spoken.append(text)
        return wav_saying(text, 22_050)


# ---- the model of the house ------------------------------------------------------------------

_SOURCES = re.compile(r"=== SOURCES (\w+) ===\n(.*?)\n=== END SOURCES \1 ===", re.DOTALL)


def answer_from(prompt: str) -> str:
    """What the stand-in chat model writes for a prompt: the first of the sources it was shown,
    quoted and cited, or that it found none. Two sentences, each long enough to be said alone."""
    sources = _SOURCES.search(prompt)
    if sources is None:
        return f"{NOT_FOUND} {OFFER}"
    _label, _, text = sources.group(2).partition("\n")
    quoted = text.split("\n\n", 1)[0].strip().rstrip(".")
    return f"From your documents: {quoted} [1]. {CLOSING}"


class HouseholdModel:
    """The chat model and the embedding model of the house, as one provider of the gateway."""

    kind = ProviderKind.OLLAMA

    def __init__(self) -> None:
        self.prompts: list[str] = []
        """What each answer was asked, as the model saw it: the sources, and the message."""

        self.readings = 0
        """How many times a message was read for a reference to the past."""

        self._words = FakeEmbedder(dimensions=64)

    def native_structured_output(self, resolved: ResolvedBinding) -> bool:
        return True

    async def chat(
        self, client: httpx.AsyncClient, resolved: ResolvedBinding, request: ChatRequest
    ) -> ChatResult:
        # What the conversation asks for whole is the reading of a message: this one stands alone.
        self.readings += 1
        reading = {"reason": "it stands alone", "kind": "none"}
        return ChatResult(text=json.dumps(reading), finish_reason=FinishReason.STOP)

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


def bindings(*, speech_to_text: bool, text_to_speech: bool) -> list[ModelBinding]:
    """What the tenant bound, the way a person writes it: the speech roles have no address."""

    def bind(
        role: ModelRole, provider: ProviderKind, model: str, base_url: str | None = None
    ) -> ModelBinding:
        return make_model_binding(
            tenant_id=TENANT, role=role, provider=provider, model=model, base_url=base_url
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


# ---- the documents of the house --------------------------------------------------------------


class Library:
    """The documents, put in through the knowledge service's own interface and read by its worker,
    which a test runs by hand: what can be found is known when ``add`` returns."""

    def __init__(self, http: httpx.AsyncClient, store: KnowledgeStore, ingestor: Ingestor) -> None:
        self._http = http
        self._store = store
        self._ingestor = ingestor

    async def add(self, collection: str, title: str, text: str, *, owner: CollectionOwner) -> None:
        created = await self._http.post(
            "/v1/collections",
            json=CollectionCreate(name=collection, owner=owner).model_dump(mode="json"),
        )
        assert created.status_code == 201, created.text
        added = await self._http.post(
            f"/v1/collections/{created.json()['id']}/documents/text",
            json=DocumentText(title=title, text=text).model_dump(mode="json", exclude_none=True),
        )
        assert added.status_code == 202, added.text
        while (job := await self._store.claim_job("e2e", lease_seconds=60)) is not None:
            await self._ingestor.run(job)
        read = await self._http.get(f"/v1/documents/{Document.model_validate(added.json()).id}")
        assert Document.model_validate(read.json()).status is DocumentStatus.READY, read.text


# ---- the whole of it -------------------------------------------------------------------------


@dataclass
class House:
    """The assistant, up: the address devices connect to, and what a test looks at."""

    edge_url: str
    sessions: Sessions
    model: HouseholdModel
    whisper: StandInWhisper
    piper: StandInPiper
    library: Library
    conversation: httpx.AsyncClient
    """The conversation service's own interface, for what it remembers."""


@asynccontextmanager
async def serving(app: FastAPI) -> AsyncIterator[str]:
    """The edge on a real socket of the loopback, as ``python -m factotum.edge`` serves it (the
    implementation of WebSocket that is not deprecated, the frame the protocol allows). What is
    given is the address a device connects to."""
    server = ReportingServer(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            log_config=None,
            ws="websockets-sansio",
            ws_max_size=MAX_FRAME_BYTES,
        )
    )
    task = asyncio.create_task(server.serve())
    try:
        await asyncio.wait_for(server.listening.wait(), TIMEOUT_S)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}/v1/connect"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, TIMEOUT_S)


async def forgotten(sessions: Sessions) -> bool:
    """Whether the edge has forgotten every device, waiting a little for it to notice."""
    for _ in range(200):
        if len(sessions) == 0:
            return True
        await asyncio.sleep(0.01)
    return False


@asynccontextmanager
async def running(
    *devices: tuple[SystemDeviceRecord, str],
    speech_to_text: bool = True,
    text_to_speech: bool = True,
) -> AsyncIterator[House]:
    """The whole assistant, with these devices registered and the models the tenant bound."""
    model, whisper, piper = HouseholdModel(), StandInWhisper(), StandInPiper()
    sessions = Sessions()
    delegation = DevDelegation(TEST)

    def authenticator() -> DevAuthenticator:
        return DevAuthenticator(TEST, tenant_id=TENANT, user_id=USER)

    async with AsyncExitStack() as closing:

        async def reach(app: FastAPI, name: str) -> httpx.AsyncClient:
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=f"http://{name}"
            )
            return await closing.enter_async_context(client)

        gateway = await reach(
            create_gateway_app(
                ModelGateway(
                    InMemoryBindingRepository(
                        bindings(speech_to_text=speech_to_text, text_to_speech=text_to_speech)
                    ),
                    EnvSecretResolver({}),
                    adapters={
                        ProviderKind.OLLAMA: model,
                        ProviderKind.WHISPER_LOCAL: WhisperLocalAdapter(
                            engine_factory=whisper.build
                        ),
                        ProviderKind.PIPER_LOCAL: PiperLocalAdapter(engine_factory=piper.build),
                    },
                    client_factory=TrackedClients(),
                    retry=RetryPolicy(attempts=2, base_delay_s=0.0, max_delay_s=0.0),
                    sleep=Sleeps(),
                    jitter=lambda: 0.0,
                ),
                authenticator(),
            ),
            "model-gateway",
        )
        embedder = GatewayEmbedder(gateway, delegation)

        documents = KnowledgeMemory()
        knowledge = await reach(
            create_knowledge_app(authenticator(), service=KnowledgeService(documents, embedder)),
            "knowledge",
        )

        chat = GatewayChat(gateway, delegation)
        store = ConversationMemory()
        history = History(store, embedder)
        engine = TurnEngine(
            store,
            chat,
            KnowledgeClient(knowledge, delegation),
            history,
            ReferenceGate(chat),
            PromptBuilder(),
        )
        conversation = await reach(
            create_conversation_app(
                authenticator(), service=ConversationService(store, engine, history)
            ),
            "conversation",
        )

        edge = create_edge_app(
            authenticator(),
            sessions=sessions,
            registry=FileDeviceRegistry(TEST, list(devices)),
            conversation=ConversationClient(conversation, DevPrincipalDelegation(TEST)),
            speech=GatewaySpeech(gateway, delegation),
            session_settings=quick_settings(idle_timeout_s=30.0),
        )
        async with serving(edge) as url:
            yield House(
                edge_url=url,
                sessions=sessions,
                model=model,
                whisper=whisper,
                piper=piper,
                library=Library(
                    knowledge,
                    documents,
                    Ingestor(documents, embedder, parser=parse_in_process),
                ),
                conversation=conversation,
            )
            assert await forgotten(sessions), "a device was still connected when the test ended"


# ---- the devices -----------------------------------------------------------------------------


def registered(name: str) -> tuple[SystemDeviceRecord, str]:
    """The device of this profile, registered as the example file has it (the phone is a person's
    own, and the others are shared)."""
    profile = get_profile(name)
    return make_device(
        name=profile.name,
        hardware_id=profile.hardware_id,
        token=profile.token,
        capabilities=profile.capabilities,
        display=profile.display,
        shared=profile.name != "phone",
    )


@dataclass
class Device:
    """A device in a terminal, on the far side of a real connection to the edge."""

    client: DeviceClient
    shown: RecordingRenderer
    speaker: FakeVoiceOutput

    def spoken(self) -> list[str]:
        """What the speaker was made to say, read back out of the clips it was asked to play."""
        return [said_in(clip) for clip in self.speaker.played]

    async def says_aloud(self, words: str) -> TurnEnded:
        """Says something with a recording of it, and waits for the answer to end."""
        return await self.sends(recording_of(words))

    async def sends(self, recording: AudioPayload) -> TurnEnded:
        return await self._answered(await self.client.say_audio(recording))

    async def types(self, words: str) -> TurnEnded:
        return await self._answered(await self.client.say(words))

    async def _answered(self, identity: str) -> TurnEnded:
        ended = await asyncio.wait_for(self.client.wait_for_turn(identity), TIMEOUT_S)
        assert ended is not None, "the connection ended before the answer did"
        return ended


@asynccontextmanager
async def joined(house: House, name: str) -> AsyncIterator[Device]:
    """A device of this profile, connected to the edge and told it is ready. It does not yet do
    what the edge sends: whoever takes it on does (``client.run()``)."""
    profile = get_profile(name)
    shown, speaker = RecordingRenderer(), FakeVoiceOutput()
    client = DeviceClient(
        await WebSocketConnection.open(house.edge_url),
        profile,
        shown,
        TerminalDevice(profile.capabilities, shown),
        voice_output=speaker,
    )
    try:
        await client.connect()
        yield Device(client, shown, speaker)
    finally:
        await client.close()


@asynccontextmanager
async def connected(house: House, name: str) -> AsyncIterator[Device]:
    """The same, doing what the edge sends."""
    async with joined(house, name) as device:
        listening = asyncio.create_task(device.client.run())
        try:
            yield device
        finally:
            await device.client.close()
            await asyncio.wait({listening})


async def until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(TIMEOUT_S):
        while not condition():  # noqa: ASYNC110 - a state to watch, there is no event
            await asyncio.sleep(0.005)


async def remembered(house: House) -> list[Exchange]:
    """What the conversation service kept of what a person said, read as that person."""
    me = {SUBJECT_HEADER: "user"}
    listed = await house.conversation.get("/v1/conversations", headers=me)
    exchanges: list[Exchange] = []
    for conversation in ConversationList.model_validate(listed.json()).conversations:
        detail = await house.conversation.get(f"/v1/conversations/{conversation.id}", headers=me)
        exchanges.extend(ConversationDetail.model_validate(detail.json()).exchanges)
    return exchanges


# ---- the tests -------------------------------------------------------------------------------


class TestAskingByVoice:
    async def test_a_spoken_question_is_answered_aloud_from_the_documents(self) -> None:
        async with running(registered("speaker")) as house:
            await house.library.add("Home", "Router manual", ROUTER, owner=CollectionOwner.TENANT)
            async with connected(house, "speaker") as speaker:
                ended = await speaker.says_aloud(QUESTION)

                assert ended.status is TurnStatus.FINISHED, ended
                # What was said was heard, with the model the binding named...
                assert (house.whisper.models, house.whisper.heard) == ([STT_MODEL], [QUESTION])
                # ...it reached the model that wrote the answer, with the manual in front of it...
                [prompt] = house.model.prompts
                assert ROUTER in prompt
                assert prompt.endswith(f"Message: {QUESTION}")
                # ...and what was written was said, a sentence at a time, in the voice that was
                # bound (what a speaker says has no references in it).
                assert speaker.shown.of("speech") == SPOKEN_ANSWER
                assert speaker.spoken() == SPOKEN_ANSWER
                assert (house.piper.voices, house.piper.spoken) == ([VOICE], speaker.spoken())
                assert speaker.shown.of("text") == []
                assert speaker.shown.of("card") == []
                assert speaker.shown.of("violation") == []

    async def test_a_screen_with_a_speaker_shows_the_answer_and_where_it_comes_from_and_says_it(
        self,
    ) -> None:
        async with running(registered("smart-display")) as house:
            await house.library.add("Home", "Router manual", ROUTER, owner=CollectionOwner.TENANT)
            async with connected(house, "smart-display") as display:
                ended = await display.says_aloud(QUESTION)

                assert ended.status is TurnStatus.FINISHED, ended
                assert " ".join(display.shown.of("text")) == f"{FROM_THE_MANUAL} [1]. {CLOSING}"
                [card] = display.shown.of("card")
                assert card.title == "Sources"
                assert card.items[0].label == "[1] Router manual"
                assert display.spoken() == display.shown.of("speech") == SPOKEN_ANSWER
                assert display.shown.of("violation") == []

    async def test_the_longest_recording_a_microphone_makes_goes_all_the_way(self) -> None:
        # Twelve seconds is where a terminal stops listening (``MicrophoneInput.max_seconds``): a
        # frame of that size is more than the whole protocol allowed before it carried audio.
        recording = recording_of(QUESTION, seconds=12)
        assert len(recording.data) > 64 * 1024
        async with running(registered("speaker")) as house:
            await house.library.add("Home", "Router manual", ROUTER, owner=CollectionOwner.TENANT)
            async with connected(house, "speaker") as speaker:
                ended = await speaker.sends(recording)

                assert ended.status is TurnStatus.FINISHED, ended
                assert house.whisper.heard == [QUESTION]
                assert speaker.spoken() == SPOKEN_ANSWER

    async def test_a_question_in_italian_keeps_its_accents_all_the_way(self) -> None:
        question = "Dov'è il router?"
        async with running(registered("speaker")) as house:
            await house.library.add(
                "Casa", "Il router", ROUTER_IN_ITALIAN, owner=CollectionOwner.TENANT
            )
            async with connected(house, "speaker") as speaker:
                ended = await speaker.says_aloud(question)

                assert ended.status is TurnStatus.FINISHED, ended
                assert house.whisper.heard == [question]
                [prompt] = house.model.prompts
                assert ROUTER_IN_ITALIAN in prompt
                assert prompt.endswith(f"Message: {question}")
                assert speaker.spoken() == [
                    f"From your documents: {ROUTER_IN_ITALIAN.rstrip('.')}.",
                    CLOSING,
                ]

    async def test_a_question_that_is_typed_is_answered_aloud_too(self) -> None:
        async with running(registered("speaker")) as house:
            await house.library.add("Home", "Router manual", ROUTER, owner=CollectionOwner.TENANT)
            async with connected(house, "speaker") as speaker:
                ended = await speaker.types(QUESTION)

                assert ended.status is TurnStatus.FINISHED, ended
                assert house.whisper.heard == []  # nothing was a recording
                assert speaker.spoken() == SPOKEN_ANSWER

    async def test_what_was_heard_is_what_the_conversation_service_remembers(self) -> None:
        async with running(registered("phone")) as house:
            await house.library.add("Home", "Router manual", ROUTER, owner=CollectionOwner.TENANT)
            async with connected(house, "phone") as phone:
                first = await phone.says_aloud(QUESTION)
                second = await phone.says_aloud("What is the reset button for?")

                assert (first.status, second.status) == (TurnStatus.FINISHED, TurnStatus.FINISHED)
                questions_and_cited = [
                    (exchange.question, [c.document_title for c in exchange.citations])
                    for exchange in sorted(await remembered(house), key=lambda e: e.seq)
                ]
                assert questions_and_cited == [
                    (QUESTION, ["Router manual"]),
                    ("What is the reset button for?", ["Router manual"]),
                ]
                # A message is read for a reference to the past only when there is a past: the
                # first was not, the second was, and the answer to it was written all the same.
                assert house.model.readings == 1
                assert len(house.model.prompts) == 2

    async def test_the_wake_word_of_the_terminal_answers_one_recording_after_another(self) -> None:
        async with running(registered("speaker")) as house:
            await house.library.add("Home", "Router manual", ROUTER, owner=CollectionOwner.TENANT)
            async with joined(house, "speaker") as speaker:
                heard = FakeVoiceInput(recording_of(QUESTION), recording_of("Where is it kept?"))
                conversing = asyncio.create_task(
                    converse_by_voice(speaker.client, heard, io.StringIO())
                )
                await until(lambda: len(speaker.shown.of("turn_ended")) == 2)
                await house.sessions.close_all()
                lost = await asyncio.wait_for(conversing, TIMEOUT_S)

                assert lost is not None
                assert lost.code == CloseCode.GOING_AWAY
                assert [ended.status for ended in speaker.shown.of("turn_ended")] == [
                    TurnStatus.FINISHED,
                    TurnStatus.FINISHED,
                ]
                assert house.whisper.heard == [QUESTION, "Where is it kept?"]
                assert speaker.spoken() == SPOKEN_ANSWER * 2


class TestWhoHearsWhat:
    async def test_a_private_document_is_read_out_by_its_owners_phone_and_never_by_the_speaker(
        self,
    ) -> None:
        async with running(registered("speaker"), registered("phone")) as house:
            await house.library.add("Private", "The safe", SAFE, owner=CollectionOwner.USER)
            async with connected(house, "speaker") as speaker, connected(house, "phone") as phone:
                await phone.says_aloud(SAFE_QUESTION)
                await speaker.says_aloud(SAFE_QUESTION)

                [for_the_phone, for_the_speaker] = house.model.prompts
                assert SAFE in for_the_phone
                assert "SOURCES" not in for_the_speaker  # nothing of it was in front of the model
                assert phone.spoken()[0] == "From your documents: The code of the safe is 4711."
                assert speaker.spoken() == [NOT_FOUND, OFFER]
                assert "4711" not in str(speaker.shown.shown)

    async def test_what_the_tenant_shares_is_read_out_by_both(self) -> None:
        async with running(registered("speaker"), registered("phone")) as house:
            await house.library.add("Home", "Router manual", ROUTER, owner=CollectionOwner.TENANT)
            async with connected(house, "speaker") as speaker, connected(house, "phone") as phone:
                await phone.says_aloud(QUESTION)
                await speaker.says_aloud(QUESTION)

                assert phone.spoken() == speaker.spoken() == SPOKEN_ANSWER


class TestWhenSomethingIsMissing:
    async def test_without_a_speech_to_text_model_the_device_is_told_and_nothing_is_asked(
        self,
    ) -> None:
        async with (
            running(registered("speaker"), speech_to_text=False) as house,
            connected(house, "speaker") as speaker,
        ):
            ended = await speaker.says_aloud(QUESTION)

            assert (ended.status, ended.code) == (TurnStatus.FAILED, "stt_not_configured")
            assert house.model.prompts == []
            assert speaker.speaker.played == []

    async def test_without_a_text_to_speech_model_the_answer_comes_without_a_voice(self) -> None:
        async with running(registered("speaker"), text_to_speech=False) as house:
            await house.library.add("Home", "Router manual", ROUTER, owner=CollectionOwner.TENANT)
            async with connected(house, "speaker") as speaker:
                ended = await speaker.says_aloud(QUESTION)

                assert ended.status is TurnStatus.FINISHED, ended
                assert speaker.shown.of("speech") == SPOKEN_ANSWER
                assert speaker.speaker.played == []

    async def test_a_recording_of_silence_is_not_a_question(self) -> None:
        async with running(registered("speaker")) as house, connected(house, "speaker") as speaker:
            ended = await speaker.says_aloud("")

            assert (ended.status, ended.code) == (TurnStatus.FAILED, "nothing_heard")
            assert house.model.prompts == []

    async def test_a_speech_model_that_falls_over_ends_that_turn_and_the_next_one_works(
        self,
    ) -> None:
        async with running(registered("speaker")) as house:
            await house.library.add("Home", "Router manual", ROUTER, owner=CollectionOwner.TENANT)
            async with connected(house, "speaker") as speaker:
                house.whisper.broken = True
                failed = await speaker.says_aloud(QUESTION)
                house.whisper.broken = False
                answered = await speaker.says_aloud(QUESTION)

                assert (failed.status, failed.code) == (TurnStatus.FAILED, "provider_unavailable")
                assert answered.status is TurnStatus.FINISHED, answered
                assert speaker.spoken() == SPOKEN_ANSWER
