"""The whole assistant, put together in one process, the way the compose file puts the services
together: the edge on a real socket, and behind it the conversation, knowledge, actions and
model-gateway applications, each reached through its own HTTP interface (an in-memory transport, so
no port is needed) and each with its store in memory instead of Postgres. Nothing above the
stores is replaced, and the home is the example one (``config/devices.example.yaml``) with the
example skills (``skills/examples``), as a person who copies the files has it.

What is made up is what a unit test cannot have, and it is at the edges: the models
(``factotum.e2e.models``), the voice (``factotum.e2e.voice``), the Home Assistant that skills reach
(``FakeWeb``, which a run is made to reach through its proxy) and the container a script runs in
(``HouseRunner``: the real script, run in a process of its own). The devices are in
``factotum.e2e.devices``.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI

from factotum.actions.api import Operations
from factotum.actions.api import create_app as create_actions_app
from factotum.actions.audit import AuditReader
from factotum.actions.catalog import Catalog
from factotum.actions.confirmations import ConfirmationReader
from factotum.actions.edge_client import EdgeDispatcher
from factotum.actions.instructions.testing import FakeWeb
from factotum.actions.instructions.writer import PlanWriter
from factotum.actions.matching import SkillIndex, SkillMatcher
from factotum.actions.planner import Planner
from factotum.actions.runner_client import RemoteRunner
from factotum.actions.service import ActionService
from factotum.contracts.actions import ActOutcome
from factotum.contracts.edge import MAX_FRAME_BYTES
from factotum.contracts.knowledge import (
    CollectionCreate,
    CollectionOwner,
    Document,
    DocumentStatus,
    DocumentText,
)
from factotum.contracts.models import ProviderKind
from factotum.conversation.actions import ActionsClient
from factotum.conversation.api import create_app as create_conversation_app
from factotum.conversation.context import PromptBuilder
from factotum.conversation.documents import KnowledgeClient
from factotum.conversation.gate import ReferenceGate
from factotum.conversation.history import History
from factotum.conversation.service import ConversationService
from factotum.conversation.turn import TurnEngine
from factotum.devices.home import parse_home
from factotum.e2e.models import HouseholdModel, bindings
from factotum.e2e.runner import HouseRunner
from factotum.e2e.speech import RealSpeech, RecordedPiper, RecordedWhisper, real_piper, real_whisper
from factotum.e2e.stores import Store, in_memory, in_postgres
from factotum.e2e.voice import PiperDouble, StandInPiper, StandInWhisper, WhisperDouble
from factotum.e2e.wiring import TENANT, USER, Auth, dev_wiring, hub_wiring
from factotum.edge.api import create_app as create_edge_app
from factotum.edge.conversation import ConversationClient
from factotum.edge.router import Sessions
from factotum.edge.testing import ReportingServer, quick_settings
from factotum.knowledge.api import create_app as create_knowledge_app
from factotum.knowledge.ingestion import Ingestor
from factotum.knowledge.service import KnowledgeService
from factotum.knowledge.store import KnowledgeStore
from factotum.knowledge.testing import parse_in_process
from factotum.model_client import GatewayChat, GatewayEmbedder, GatewaySpeech
from factotum.model_gateway.api import create_app as create_gateway_app
from factotum.model_gateway.core import ModelGateway
from factotum.model_gateway.providers.piper_local import PiperLocalAdapter
from factotum.model_gateway.providers.whisper_local import WhisperLocalAdapter
from factotum.model_gateway.retry import RetryPolicy
from factotum.model_gateway.secrets import EnvSecretResolver
from factotum.model_gateway.testing import Sleeps, TrackedClients

DEFAULT_STORE = Store.MEMORY
"""Where the services keep what they keep unless a test says: the tests of ``e2e/tests`` run on
each (see its ``conftest.py``), which is what changes this."""

TIMEOUT_S = 10.0
"""What a test waits for something that should be immediate: a stuck one ends instead of hanging."""

_ROOT = Path(__file__).resolve().parents[4]
HOME_FILE = _ROOT / "config" / "devices.example.yaml"
SKILLS_FOLDER = _ROOT / "skills" / "examples"

HOME_ASSISTANT = (
    "/api/services/light/turn_on",
    "/api/services/light/turn_off",
    "/api/services/cover/open_cover",
    "/api/services/cover/close_cover",
)
"""The calls of the example skills to Home Assistant, which the stand-in answers."""

OPEN_METEO = (
    ("/v1/search", {"results": [{"name": "the place", "latitude": 45.07, "longitude": 7.69}]}),
    ("/v1/air-quality", {"current": {"european_aqi": 21}}),
)
"""What the plan of a skill that reads the air asks for, and what the stand-in answers: where a
place is, and how good the air is there."""


class Library:
    """The documents, put in through the knowledge service's own interface and read by its worker,
    which a test runs by hand: what can be found is known when ``add`` returns. A text is read where
    the test is; a file (``upload``) is read the way the service reads it, in a process of its own
    that has nothing but the bytes and a limit of time and memory."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        store: KnowledgeStore,
        ingestor: Ingestor,
        isolated: Ingestor,
        admin: dict[str, str],
    ) -> None:
        self._http = http
        self._store = store
        self._ingestor = ingestor
        self._isolated = isolated
        self._admin = admin

    async def add(self, collection: str, title: str, text: str, *, owner: CollectionOwner) -> None:
        created = await self._collection(collection, owner)
        added = await self._http.post(
            f"/v1/collections/{created}/documents/text",
            json=DocumentText(title=title, text=text).model_dump(mode="json", exclude_none=True),
            headers=self._admin,
        )
        assert added.status_code == 202, added.text
        document = await self._read(self._ingestor, Document.model_validate(added.json()))
        assert document.status is DocumentStatus.READY, document

    async def upload(
        self,
        collection: str,
        filename: str,
        content: bytes,
        media_type: str,
        *,
        owner: CollectionOwner,
        title: str | None = None,
    ) -> Document:
        """A file, put in as a person does it, and what became of it once it was read."""
        created = await self._collection(collection, owner)
        uploaded = await self._http.post(
            f"/v1/collections/{created}/documents",
            files={"file": (filename, content, media_type)},
            data={"title": title} if title else None,
            headers=self._admin,
        )
        assert uploaded.status_code == 202, uploaded.text
        return await self._read(self._isolated, Document.model_validate(uploaded.json()))

    async def _collection(self, name: str, owner: CollectionOwner) -> str:
        created = await self._http.post(
            "/v1/collections",
            json=CollectionCreate(name=name, owner=owner).model_dump(mode="json"),
            headers=self._admin,
        )
        assert created.status_code == 201, created.text
        return str(created.json()["id"])

    async def _read(self, ingestor: Ingestor, document: Document) -> Document:
        while (job := await self._store.claim_job("e2e", lease_seconds=60)) is not None:
            await ingestor.run(job)
        read = await self._http.get(f"/v1/documents/{document.id}", headers=self._admin)
        return Document.model_validate(read.json())


@dataclass
class House:
    """The assistant, up: the address devices connect to, and what a test looks at."""

    edge_url: str
    sessions: Sessions
    model: HouseholdModel
    whisper: WhisperDouble
    """The engine behind speech-to-text: the stand-in, or the real one under a recorder."""

    piper: PiperDouble
    timeout_s: float
    """What a test waits for something that should be immediate: more where real speech runs."""

    library: Library
    conversation: httpx.AsyncClient
    """The conversation service's own interface, for what it remembers."""

    actions: httpx.AsyncClient
    """The actions service's own interface, for its skills and its audit trail."""

    home_assistant: FakeWeb
    """What the skills that switch a lamp or a door reach: it keeps what it was asked."""

    runner: HouseRunner
    """What ran a skill: the jobs it was given, and what they printed."""

    acts: list[ActOutcome]
    """What the actions service said of each message it was asked about, in order: what became of
    it (``ActStatus``), and the skill that was chosen. Every message that a person says is asked
    about, whether or not it turns out to be a request."""

    admin: dict[str, str]
    """The headers of the administrator that the tests act as, in the way the services check."""

    tokens: dict[str, str]
    """The credential each device was given when it was paired, by hardware id (the hub only)."""

    secret: bytes | None
    """The secret that signs the tokens (the hub only), for a test that makes one of its own."""

    hub: httpx.AsyncClient | None
    """The hub's own interface (the hub only)."""

    services: dict[str, httpx.AsyncClient]
    """The own interface of every service that has one, by name, as another service reaches it."""


@dataclass(frozen=True)
class Address:
    websocket: str
    http: str


@asynccontextmanager
async def serving(app: FastAPI) -> AsyncIterator[Address]:
    """The edge on a real socket of the loopback, as ``python -m factotum.edge`` serves it (the
    implementation of WebSocket that is not deprecated, the frame the protocol allows)."""
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
        yield Address(f"ws://127.0.0.1:{port}/v1/connect", f"http://127.0.0.1:{port}")
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


def vocabulary_of(speech: RealSpeech, given: tuple[str, ...] | None) -> tuple[str, ...] | None:
    """The words that the binding of speech-to-text lists for the real Whisper: what a test says,
    or else what a person trying words on the models of this machine said
    (``FACTOTUM_LIVE_VOCABULARY``), or nothing."""
    return given if given is not None else (speech.vocabulary or None)


@asynccontextmanager
async def running(
    *,
    auth: Auth = Auth.DEV,
    storage: Store | None = None,
    speech_to_text: bool = True,
    text_to_speech: bool = True,
    speech: RealSpeech | None = None,
    timeout_s: float = TIMEOUT_S,
    stream_stall_s: float | None = None,
    offer_every_skill: bool = False,
    vocabulary: tuple[str, ...] | None = None,
) -> AsyncIterator[House]:
    """The whole assistant, in the example home, with the models the tenant bound. ``auth`` is how
    the services trust each other: the headers of development, or the hub's signed tokens, with
    the home paired in a hub of this process (``factotum.e2e.wiring``). ``storage`` is where they
    keep what they keep (``factotum.e2e.stores``). ``speech`` puts the real Whisper and the real
    Piper where the stand-ins are (``factotum.e2e.speech``), bound by the paths of their models.
    ``stream_stall_s`` is how long the edge waits for the next chunk of what a person is saying
    before it gives up on it, where a test is about that. ``offer_every_skill`` has the planner
    shown every skill whatever the message is: the stand-in that gives words a direction cannot tell
    that "bump it up to eighty" is near the volume, and a series of commands is about what is done
    with what the planner proposes, and not about which skills the words were near.
    ``vocabulary`` is the words that the speech-to-text binding lists for the model to write as they
    are."""
    model = HouseholdModel()
    whisper: WhisperDouble
    piper: PiperDouble
    if speech is None:
        whisper, piper = StandInWhisper(), StandInPiper()
        models = bindings(
            TENANT,
            speech_to_text=speech_to_text,
            text_to_speech=text_to_speech,
            vocabulary=vocabulary,
        )
    else:
        whisper = RecordedWhisper(real_whisper(speech))
        piper = RecordedPiper(real_piper(speech))
        models = bindings(
            TENANT,
            speech_to_text=speech_to_text,
            text_to_speech=text_to_speech,
            whisper_model=str(speech.whisper),
            voice=str(speech.voice),
            languages=speech.languages,
            vocabulary=vocabulary_of(speech, vocabulary),
        )
    home = parse_home(
        HOME_FILE.read_text(encoding="utf-8"),
        tenant_id=TENANT,
        user_id=USER,
        source=HOME_FILE.name,
    )
    sessions = Sessions()

    async with AsyncExitStack() as closing:

        async def reach(app: FastAPI, name: str) -> httpx.AsyncClient:
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=f"http://{name}"
            )
            return await closing.enter_async_context(client)

        stores = (
            in_memory()
            if (storage or DEFAULT_STORE) is Store.MEMORY
            else await in_postgres(closing)
        )
        wiring = (
            dev_wiring(home, models)
            if auth is Auth.DEV
            else await hub_wiring(home, models, reach, stores.hub)
        )

        gateway = await reach(
            create_gateway_app(
                ModelGateway(
                    wiring.repository,
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
                wiring.authenticator(),
            ),
            "model-gateway",
        )
        embedder = GatewayEmbedder(gateway, wiring.delegation)
        chat = GatewayChat(gateway, wiring.delegation)

        documents = stores.knowledge
        knowledge = await reach(
            create_knowledge_app(
                wiring.authenticator(), service=KnowledgeService(documents, embedder)
            ),
            "knowledge",
        )

        # What a skill reaches is a web server on this machine that every host resolves to, and what
        # runs it is its own script in a process of its own: the container is what is not here.
        home_assistant = closing.enter_context(FakeWeb())
        for path in HOME_ASSISTANT:
            home_assistant.answer("POST", path, json_body=[])
        for path, found in OPEN_METEO:
            home_assistant.answer("GET", path, json_body=found)
        local_runner = HouseRunner(proxy=home_assistant.url())
        skills = stores.actions
        index = SkillIndex(skills, embedder, timeout_s=TIMEOUT_S)
        closing.push_async_callback(index.aclose)
        matcher = (
            SkillMatcher(
                skills, embedder, index, timeout_s=TIMEOUT_S, threshold=0.0, max_candidates=20
            )
            if offer_every_skill
            else SkillMatcher(skills, embedder, index, timeout_s=TIMEOUT_S)
        )
        runner_client = httpx.AsyncClient(
            transport=local_runner.transport(), base_url="http://runner.test"
        )
        await closing.enter_async_context(runner_client)
        # The edge has no address until it is listening, and the actions service reaches devices
        # through it: the client is given its address then.
        edge_client = httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_S))
        await closing.enter_async_context(edge_client)
        actions = await reach(
            create_actions_app(
                wiring.authenticator(),
                operations=Operations(
                    service=ActionService(
                        store=skills,
                        directory=wiring.directory,
                        matcher=matcher,
                        planner=Planner(chat, timeout_s=TIMEOUT_S),
                        writer=PlanWriter(chat, timeout_s=TIMEOUT_S),
                        reader=ConfirmationReader(chat, timeout_s=TIMEOUT_S),
                        runner=RemoteRunner(
                            runner_client,
                            store=skills,
                            secrets=wiring.secrets,
                            delegation=wiring.principals,
                        ),
                        dispatcher=EdgeDispatcher(edge_client, wiring.principals),
                    ),
                    catalog=Catalog(skills, index, folder=SKILLS_FOLDER, enable_folder=True),
                    audit=AuditReader(skills),
                ),
            ),
            "actions",
        )
        reloaded = await actions.post("/v1/skills/reload", headers=wiring.admin)
        assert reloaded.status_code == 200, reloaded.text

        acts: list[ActOutcome] = []

        async def keep(response: httpx.Response) -> None:
            if response.request.url.path == "/v1/act" and response.status_code == 200:
                await response.aread()
                acts.append(ActOutcome.model_validate_json(response.content))

        actions.event_hooks["response"].append(keep)

        store = stores.conversation
        history = History(store, embedder)
        engine = TurnEngine(
            store,
            chat,
            KnowledgeClient(knowledge, wiring.delegation),
            history,
            ReferenceGate(chat),
            PromptBuilder(),
            actions=ActionsClient(actions, wiring.principals),
        )
        conversation = await reach(
            create_conversation_app(
                wiring.authenticator(), service=ConversationService(store, engine, history)
            ),
            "conversation",
        )

        edge = create_edge_app(
            wiring.authenticator(),
            sessions=sessions,
            registry=wiring.registry,
            conversation=ConversationClient(conversation, wiring.principals),
            speech=GatewaySpeech(gateway, wiring.delegation),
            session_settings=quick_settings(
                idle_timeout_s=30.0,
                command_timeout_s=timeout_s,
                send_timeout_s=timeout_s,
                **({} if stream_stall_s is None else {"stream_stall_s": stream_stall_s}),
            ),
        )
        async with serving(edge) as address:
            edge_client.base_url = httpx.URL(address.http)
            yield House(
                edge_url=address.websocket,
                sessions=sessions,
                model=model,
                whisper=whisper,
                piper=piper,
                timeout_s=timeout_s,
                library=Library(
                    knowledge,
                    documents,
                    Ingestor(documents, embedder, parser=parse_in_process),
                    Ingestor(documents, embedder),
                    wiring.admin,
                ),
                conversation=conversation,
                actions=actions,
                home_assistant=home_assistant,
                runner=local_runner,
                acts=acts,
                admin=wiring.admin,
                tokens=wiring.tokens,
                secret=wiring.secret,
                hub=wiring.hub,
                services={
                    "model-gateway": gateway,
                    "knowledge": knowledge,
                    "conversation": conversation,
                    "actions": actions,
                    **({} if wiring.hub is None else {"hub": wiring.hub}),
                },
            )
            assert await forgotten(sessions), "a device was still connected when the test ended"
