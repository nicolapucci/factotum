"""The whole assistant, put together in one process, the way the compose file puts the services
together: the edge on a real socket, and behind it the conversation, knowledge, actions and
model-gateway applications, each reached through its own HTTP interface (an in-memory transport, so
no port is needed) and each with its store in memory instead of Postgres. Nothing above the
stores is replaced, and the home is the example one (``config/devices.example.yaml``) with the
example skills (``skills/examples``), as a person who copies the files has it.

What is made up is what a unit test cannot have, and it is at the edges: the models
(``factotum.e2e.models``), the voice (``factotum.e2e.voice``), the Home Assistant that skills reach
(``FakeWeb``, which a run is made to reach through its proxy) and the container a script runs in
(``LocalRunner``: the real script, run in a process of its own). The devices are in
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
from factotum.actions.directory import FileDeviceDirectory
from factotum.actions.edge_client import EdgeDispatcher
from factotum.actions.instructions.testing import FakeWeb, LocalRunner
from factotum.actions.matching import SkillIndex, SkillMatcher
from factotum.actions.planner import Planner
from factotum.actions.runner_client import RemoteRunner
from factotum.actions.secrets import FileSecrets, parse_secrets
from factotum.actions.service import ActionService
from factotum.actions.store import InMemoryStore as ActionsMemory
from factotum.common.config import Environment, ServiceSettings
from factotum.common.devauth import (
    DEV_TENANT_ID,
    DEV_USER_ID,
    DevAuthenticator,
    DevPrincipalDelegation,
)
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
from factotum.conversation.store import InMemoryStore as ConversationMemory
from factotum.conversation.turn import TurnEngine
from factotum.devices.home import parse_home
from factotum.e2e.models import HouseholdModel, bindings
from factotum.e2e.voice import StandInPiper, StandInWhisper
from factotum.edge.api import create_app as create_edge_app
from factotum.edge.conversation import ConversationClient
from factotum.edge.registry import FileDeviceRegistry
from factotum.edge.router import Sessions
from factotum.edge.testing import ReportingServer, quick_settings
from factotum.knowledge.api import create_app as create_knowledge_app
from factotum.knowledge.ingestion import Ingestor
from factotum.knowledge.service import KnowledgeService
from factotum.knowledge.store import InMemoryStore as KnowledgeMemory
from factotum.knowledge.store import KnowledgeStore
from factotum.knowledge.testing import parse_in_process
from factotum.model_client import DevDelegation, GatewayChat, GatewayEmbedder, GatewaySpeech
from factotum.model_gateway.api import create_app as create_gateway_app
from factotum.model_gateway.bindings import InMemoryBindingRepository
from factotum.model_gateway.core import ModelGateway
from factotum.model_gateway.providers.piper_local import PiperLocalAdapter
from factotum.model_gateway.providers.whisper_local import WhisperLocalAdapter
from factotum.model_gateway.retry import RetryPolicy
from factotum.model_gateway.secrets import EnvSecretResolver
from factotum.model_gateway.testing import Sleeps, TrackedClients

TIMEOUT_S = 10.0
"""What a test waits for something that should be immediate: a stuck one ends instead of hanging."""

TENANT = DEV_TENANT_ID
USER = DEV_USER_ID
"""The tenant and the person of every service in development, so that what one of them is told about
whom a device acts for is what the next one understands."""

TEST = ServiceSettings(_env_file=None, environment=Environment.TEST)

_ROOT = Path(__file__).resolve().parents[4]
HOME_FILE = _ROOT / "config" / "devices.example.yaml"
SKILLS_FOLDER = _ROOT / "skills" / "examples"

HA_TOKEN = "the-token-of-the-home"  # noqa: S105 - a stand-in, in the secrets the example file shows
SECRETS = f'tenant:\n  HA_TOKEN: "{HA_TOKEN}"\n'
"""What the home keeps for its skills, as ``config/secrets.example.yaml`` has it."""

HOME_ASSISTANT = (
    "/api/services/light/turn_on",
    "/api/services/light/turn_off",
    "/api/services/cover/open_cover",
    "/api/services/cover/close_cover",
)
"""The calls of the example skills to Home Assistant, which the stand-in answers."""


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

    actions: httpx.AsyncClient
    """The actions service's own interface, for its skills and its audit trail."""

    home_assistant: FakeWeb
    """What the skills that switch a lamp or a door reach: it keeps what it was asked."""

    runner: LocalRunner
    """What ran a skill: the jobs it was given, and what they printed."""


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


def authenticator() -> DevAuthenticator:
    return DevAuthenticator(TEST, tenant_id=TENANT, user_id=USER)


@asynccontextmanager
async def running(
    *, speech_to_text: bool = True, text_to_speech: bool = True
) -> AsyncIterator[House]:
    """The whole assistant, in the example home, with the models the tenant bound."""
    model, whisper, piper = HouseholdModel(), StandInWhisper(), StandInPiper()
    home = parse_home(
        HOME_FILE.read_text(encoding="utf-8"),
        tenant_id=TENANT,
        user_id=USER,
        source=HOME_FILE.name,
    )
    sessions = Sessions()
    delegation = DevDelegation(TEST)
    principals = DevPrincipalDelegation(TEST)

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
                        bindings(
                            TENANT, speech_to_text=speech_to_text, text_to_speech=text_to_speech
                        )
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
        chat = GatewayChat(gateway, delegation)

        documents = KnowledgeMemory()
        knowledge = await reach(
            create_knowledge_app(authenticator(), service=KnowledgeService(documents, embedder)),
            "knowledge",
        )

        # What a skill reaches is a web server on this machine that every host resolves to, and what
        # runs it is its own script in a process of its own: the container is what is not here.
        home_assistant = closing.enter_context(FakeWeb())
        for path in HOME_ASSISTANT:
            home_assistant.answer("POST", path, json_body=[])
        local_runner = LocalRunner(proxy=home_assistant.url())
        skills = ActionsMemory()
        index = SkillIndex(skills, embedder, timeout_s=TIMEOUT_S)
        closing.push_async_callback(index.aclose)
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
                authenticator(),
                operations=Operations(
                    service=ActionService(
                        store=skills,
                        directory=FileDeviceDirectory(TEST, home),
                        matcher=SkillMatcher(skills, embedder, index, timeout_s=TIMEOUT_S),
                        planner=Planner(chat, timeout_s=TIMEOUT_S),
                        reader=ConfirmationReader(chat, timeout_s=TIMEOUT_S),
                        runner=RemoteRunner(
                            runner_client,
                            store=skills,
                            secrets=FileSecrets(
                                TEST,
                                parse_secrets(SECRETS, tenant_id=TENANT, user_id=USER),
                                tenant_id=TENANT,
                            ),
                            delegation=principals,
                        ),
                        dispatcher=EdgeDispatcher(edge_client, principals),
                    ),
                    catalog=Catalog(skills, index, folder=SKILLS_FOLDER, enable_folder=True),
                    audit=AuditReader(skills),
                ),
            ),
            "actions",
        )
        reloaded = await actions.post("/v1/skills/reload")
        assert reloaded.status_code == 200, reloaded.text

        store = ConversationMemory()
        history = History(store, embedder)
        engine = TurnEngine(
            store,
            chat,
            KnowledgeClient(knowledge, delegation),
            history,
            ReferenceGate(chat),
            PromptBuilder(),
            actions=ActionsClient(actions, principals),
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
            registry=FileDeviceRegistry(TEST, list(home.system)),
            conversation=ConversationClient(conversation, principals),
            speech=GatewaySpeech(gateway, delegation),
            session_settings=quick_settings(
                idle_timeout_s=30.0, command_timeout_s=TIMEOUT_S, send_timeout_s=TIMEOUT_S
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
                library=Library(
                    knowledge,
                    documents,
                    Ingestor(documents, embedder, parser=parse_in_process),
                ),
                conversation=conversation,
                actions=actions,
                home_assistant=home_assistant,
                runner=local_runner,
            )
            assert await forgotten(sessions), "a device was still connected when the test ended"
