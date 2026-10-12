"""The conversation service against real models: how well a small model tells whether a message
refers to what was said before, and a whole conversation with real documents in a real database.

Opt-in, because it needs Docker's Postgres and a running Ollama with the models pulled:

    FACTOTUM_POSTGRES_PASSWORD=dev FACTOTUM_LIVE_TESTS=1 \\
        uv run pytest -m live services/conversation/tests/live -s

``-s`` shows what was measured. Which models to use can be changed:

* FACTOTUM_LIVE_OLLAMA_URL: where Ollama is (http://127.0.0.1:11434).
* FACTOTUM_LIVE_MAIN_MODEL: the model that answers (llama3.1:8b-instruct-q4_K_M).
* FACTOTUM_LIVE_FAST_MODEL: the model that reads whether a message refers to the past (the same).
* FACTOTUM_LIVE_GATE_MODELS: the models the gate is measured with, separated by commas.
* FACTOTUM_LIVE_EMBED_MODEL: the embedding model (bge-m3).

Two models that do not fit in the memory of the graphics card together make Ollama unload one to
load the other, and every turn then pays for it, so use one model for both roles unless there is
room. The default is an 8B model because a short follow-up such as "and in Italian?" is answered
differently from one time to the next by a smaller one (docs/conversation.md): what the last tests
check is the design, not how well a model of 3B translates.
"""

import asyncio
import os
import statistics
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest
import pytest_asyncio

from factotum.common.config import Environment, ServiceSettings
from factotum.common.devauth import DEV_TENANT_ID, DEV_USER_ID, DevAuthenticator
from factotum.common.urlguard import UrlPolicy
from factotum.contracts.chat import ChatMessage, ChatRequest, MessageRole
from factotum.contracts.conversation import (
    Delta,
    Finished,
    HistoryQuery,
    Reference,
    ReferenceKind,
    TurnEvent,
    TurnRequest,
)
from factotum.contracts.identity import Owner, Role
from factotum.contracts.knowledge import CollectionCreate, DocumentStatus, DocumentText
from factotum.contracts.models import BindingOptions, ModelRole, ProviderKind
from factotum.contracts.testing import make_model_binding, make_user_principal
from factotum.conversation.context import PromptBuilder
from factotum.conversation.documents import KnowledgeClient
from factotum.conversation.gate import ReferenceGate
from factotum.conversation.gate_cases import GATE_CASES, GateCase
from factotum.conversation.history import History
from factotum.conversation.service import ConversationService
from factotum.conversation.store.postgres import PostgresStore, prepare
from factotum.conversation.turn import TurnEngine
from factotum.db.pool import Database
from factotum.db.settings import DatabaseSettings
from factotum.knowledge.api import create_app as create_knowledge_app
from factotum.knowledge.ingestion import IngestionSettings, Ingestor
from factotum.knowledge.service import KnowledgeService
from factotum.knowledge.store.postgres import PostgresStore as KnowledgeStore
from factotum.knowledge.store.postgres import prepare as prepare_knowledge
from factotum.knowledge.worker import Worker
from factotum.model_client import DevDelegation, GatewayChat, GatewayEmbedder
from factotum.model_gateway.api import create_app as create_gateway_app
from factotum.model_gateway.bindings import InMemoryBindingRepository
from factotum.model_gateway.core import ModelGateway, guarded_clients
from factotum.model_gateway.secrets import EnvSecretResolver

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("FACTOTUM_LIVE_TESTS") != "1",
        reason="live tests are opt-in: set FACTOTUM_LIVE_TESTS=1 (and run Ollama and Postgres)",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

URL = os.environ.get("FACTOTUM_LIVE_OLLAMA_URL", "http://127.0.0.1:11434")
MAIN_MODEL = os.environ.get("FACTOTUM_LIVE_MAIN_MODEL", "llama3.1:8b-instruct-q4_K_M")
FAST_MODEL = os.environ.get("FACTOTUM_LIVE_FAST_MODEL", MAIN_MODEL)
EMBED_MODEL = os.environ.get("FACTOTUM_LIVE_EMBED_MODEL", "bge-m3")
GATE_MODELS = [
    name.strip()
    for name in os.environ.get("FACTOTUM_LIVE_GATE_MODELS", FAST_MODEL).split(",")
    if name.strip()
]

NUM_CTX = 8704
"""The same window for every role, so that one model bound to two of them is one model to Ollama:
a window that differs (an output limit that does) loads it again each time it is asked for in the
other role, and a turn that takes a second takes fourteen."""

OWNER = Owner.model_validate({"kind": "user", "id": str(DEV_USER_ID)})
PRINCIPAL = make_user_principal(
    tenant_id=DEV_TENANT_ID, user_id=DEV_USER_ID, roles=frozenset({Role.ADMIN})
)


def binding(role: ModelRole, model: str, *, output: int) -> object:
    return make_model_binding(
        tenant_id=DEV_TENANT_ID,
        role=role,
        provider=ProviderKind.OLLAMA,
        model=model,
        base_url=URL,
        options=BindingOptions(
            timeout_s=300, max_input_tokens=8192, max_output_tokens=output, num_ctx=NUM_CTX
        ),
    )


@dataclass
class Gateway:
    """A gateway in this process, over real Ollama, and the clients that reach it."""

    chat: GatewayChat
    embedder: GatewayEmbedder
    http: httpx.AsyncClient
    gateway: ModelGateway


async def open_gateway(main: str, fast: str) -> Gateway:
    settings = ServiceSettings(environment=Environment.TEST, _env_file=None)
    bindings = [
        binding(ModelRole.CHAT_MAIN, main, output=512),
        binding(ModelRole.CHAT_FAST, fast, output=120),
        make_model_binding(
            tenant_id=DEV_TENANT_ID,
            role=ModelRole.EMBEDDING,
            provider=ProviderKind.OLLAMA,
            model=EMBED_MODEL,
            base_url=URL,
            options=BindingOptions(timeout_s=300),
        ),
    ]
    gateway = ModelGateway(
        InMemoryBindingRepository(bindings),  # type: ignore[arg-type]
        EnvSecretResolver(),
        client_factory=guarded_clients(UrlPolicy.private_networks_allowed()),
    )
    app = create_gateway_app(
        gateway, DevAuthenticator(settings, tenant_id=DEV_TENANT_ID, user_id=DEV_USER_ID)
    )
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://gateway",
        timeout=httpx.Timeout(300.0),
    )
    delegation = DevDelegation(settings)
    return Gateway(
        chat=GatewayChat(http, delegation),
        embedder=GatewayEmbedder(http, delegation, batch=16),
        http=http,
        gateway=gateway,
    )


async def close_gateway(opened: Gateway) -> None:
    await opened.http.aclose()
    await opened.gateway.aclose()


# ---- how well a small model tells whether a message refers to the past ---------------------------

NONE = ReferenceKind.NONE
CONT = ReferenceKind.CONTINUATION
EARLIER = ReferenceKind.EARLIER


@pytest.mark.parametrize("model", GATE_MODELS)
async def test_a_small_model_tells_whether_a_message_refers_to_the_past(model: str) -> None:
    opened = await open_gateway(model, model)
    try:
        gate = ReferenceGate(opened.chat, timeout_s=120)
        # The first call loads the model: it is not what a turn pays for, so it is not measured.
        await gate.decide(OWNER, "warm up")

        seconds: list[float] = []
        decided: list[tuple[GateCase, Reference | None]] = []
        for case in GATE_CASES:
            began = time.perf_counter()
            try:
                reference: Reference | None = await gate.decide(OWNER, case.message)
            except Exception as error:  # noqa: BLE001 - what is measured is how often it fails
                print(f"  failed on {case.message!r}: {type(error).__name__}: {error}")
                reference = None
            seconds.append(time.perf_counter() - began)
            decided.append((case, reference))
    finally:
        await close_gateway(opened)

    print(f"\n  gate model {model}: {len(GATE_CASES)} messages")
    print(f"  {'':<14} {'right':>6} {'of':>4}")
    scores: dict[ReferenceKind, tuple[int, int]] = {}
    for kind in ReferenceKind:
        cases = [(c, r) for c, r in decided if c.kind is kind]
        right = sum(1 for c, r in cases if r is not None and r.kind is c.kind)
        scores[kind] = (right, len(cases))
        print(f"  {kind.value:<14} {right:>6} {len(cases):>4}")
    earlier = [
        (c, r) for c, r in decided if c.kind is EARLIER and r is not None and r.kind is EARLIER
    ]
    when_right = sum(1 for c, r in earlier if c.when == r.when)
    topic_right = sum(
        1
        for c, r in earlier
        if not c.topic or (r.topic and any(word in r.topic.casefold() for word in c.topic))
    )
    print(
        f"  of the earlier ones found: the period is right for {when_right}/{len(earlier)}, "
        f"the topic for {topic_right}/{len(earlier)}"
    )
    print(
        f"  time for one message: median {statistics.median(seconds) * 1000:.0f} ms, "
        f"90th percentile {sorted(seconds)[int(len(seconds) * 0.9)] * 1000:.0f} ms, "
        f"slowest {max(seconds) * 1000:.0f} ms"
    )
    for case, reference in decided:
        if reference is None or reference.kind is not case.kind:
            got = "nothing" if reference is None else reference.kind.value
            print(f"  wrong: {case.message!r} is {case.kind.value}, said {got}")

    total = sum(right for right, _ in scores.values())
    # What must hold of any model worth binding to the fast role: it does not send the past along
    # with what stands alone (that is the cost this whole design is there to avoid), it does not
    # miss the messages that cannot be understood without it, and it is right about most of them.
    assert scores[NONE][0] / scores[NONE][1] >= 0.85
    assert scores[CONT][0] / scores[CONT][1] >= 0.7
    assert scores[EARLIER][0] / scores[EARLIER][1] >= 0.7
    assert total / len(GATE_CASES) >= 0.85


# ---- a whole conversation --------------------------------------------------------------------

LIBRARY = {
    "Router manual": (
        "# Router\n\n## Factory reset\n\nTo restore the factory settings of the router, hold the "
        "reset button for ten seconds until the light blinks. All custom settings, including the "
        "WiFi password, will be erased.\n\n## Firmware\n\nThe firmware is updated from the "
        "administration page. Do not switch the router off during the update."
    ),
    "Warranty": (
        "The warranty covers manufacturing defects for two years from the day of purchase. Damage "
        "caused by water, drops or unauthorized repairs is not covered. To claim the warranty keep "
        "the receipt."
    ),
    "Sourdough recipe": (
        "To make sourdough bread, mix flour, water and salt with an active starter. Let the dough "
        "rest overnight, then bake it in a hot oven with steam for forty minutes."
    ),
    "Backup guide": (
        "Backups should follow the 3-2-1 rule: three copies of your data, on two different media, "
        "with one copy stored off site. Test restoring from a backup at least once a year."
    ),
}


@dataclass
class Talk:
    events: list[TurnEvent]
    seconds: float
    first_word: float

    @property
    def said(self) -> str:
        return "".join(e.text for e in self.events if isinstance(e, Delta))

    @property
    def finished(self) -> Finished:
        [done] = [e for e in self.events if isinstance(e, Finished)]
        return done


@dataclass
class Conversation:
    service: ConversationService
    history: History

    async def say(self, message: str) -> Talk:
        began = time.perf_counter()
        first_word = 0.0
        events: list[TurnEvent] = []
        async for event in self.service.turn(PRINCIPAL, TurnRequest(message=message)):
            if isinstance(event, Delta) and not first_word:
                first_word = time.perf_counter() - began
            events.append(event)
        talk = Talk(events, time.perf_counter() - began, first_word)
        print(
            f"  {message!r} -> {talk.finished.reference.value}, "
            f"{talk.finished.exchanges_used} exchange(s) and {talk.finished.passages_used} "
            f"passage(s); first word after {talk.first_word * 1000:.0f} ms, "
            f"{talk.seconds * 1000:.0f} ms in all"
        )
        print(f"    {talk.said.strip()!r}")
        return talk


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def conversation(database_settings: DatabaseSettings) -> AsyncIterator[Conversation]:
    opened = await open_gateway(MAIN_MODEL, FAST_MODEL)
    database = await Database.connect(database_settings)
    await prepare(database)
    await prepare_knowledge(database)

    # The documents: the real knowledge service, in this process, over the same gateway.
    settings = ServiceSettings(environment=Environment.TEST, _env_file=None)
    knowledge_store = KnowledgeStore(database)
    ingestor = Ingestor(knowledge_store, opened.embedder, settings=IngestionSettings())
    stop = asyncio.Event()
    working = asyncio.create_task(
        Worker(knowledge_store, ingestor, concurrency=2, poll_seconds=0.2).run(stop)
    )
    documents = KnowledgeService(knowledge_store, opened.embedder)
    knowledge_http = httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=create_knowledge_app(
                DevAuthenticator(settings, tenant_id=DEV_TENANT_ID, user_id=DEV_USER_ID),
                service=documents,
            )
        ),
        base_url="http://knowledge",
        timeout=httpx.Timeout(300.0),
    )
    try:
        collection = await documents.create_collection(PRINCIPAL, CollectionCreate(name="Manuals"))
        added = [
            await documents.add_text(
                PRINCIPAL, collection.id, DocumentText(title=title, text=text, language="en")
            )
            for title, text in LIBRARY.items()
        ]
        while True:
            current = [await documents.get_document(PRINCIPAL, d.id) for d in added]
            if all(d.status in {DocumentStatus.READY, DocumentStatus.FAILED} for d in current):
                break
            await asyncio.sleep(0.5)
        assert all(d.status is DocumentStatus.READY for d in current)

        store = PostgresStore(database)
        history = History(store, opened.embedder, min_similarity=0.5)
        engine = TurnEngine(
            store,
            opened.chat,
            KnowledgeClient(knowledge_http, DevDelegation(settings)),
            history,
            ReferenceGate(opened.chat, timeout_s=120),
            PromptBuilder(),
        )
        # Warm the models, so that what is measured is a turn and not the loading of a model.
        await opened.chat.chat(
            OWNER,
            ChatRequest(
                role=ModelRole.CHAT_MAIN,
                messages=(ChatMessage(role=MessageRole.USER, content="Say hello."),),
                max_output_tokens=5,
            ),
        )
        await opened.embedder.embed(OWNER, ["warm up"])
        yield Conversation(ConversationService(store, engine, history), history)
    finally:
        stop.set()
        await asyncio.wait_for(working, 60)
        await knowledge_http.aclose()
        await close_gateway(opened)
        await database.close()


async def test_a_conversation_carries_no_history_until_a_message_refers_to_it(
    conversation: Conversation,
) -> None:
    print(f"\n  answers: {MAIN_MODEL}; reads the messages: {FAST_MODEL}; embeds: {EMBED_MODEL}")

    # Nobody has said anything yet: nothing to refer to, and so nobody asks.
    first = await conversation.say("How do I restore my router to factory settings?")
    assert first.finished.reference is NONE
    assert first.finished.passages_used >= 1
    assert "reset" in first.said.lower()
    assert first.finished.untrusted_content is True
    assert [e.type for e in first.events].count("citations") <= 1

    # It carries on the exchange just before: without it "it" is nothing, and the answer is the
    # router's. (What a model of 3B makes of "and in Italian?" varies from one answer to the next.)
    second = await conversation.say("And how long should I hold it?")
    assert second.finished.reference is CONT
    assert second.finished.exchanges_used == 1
    assert "ten seconds" in second.said.lower() or "10" in second.said

    # Something else entirely: the past is not sent along, and the answer is about the warranty.
    third = await conversation.say("What does the warranty not cover?")
    assert third.finished.reference is NONE
    assert third.finished.exchanges_used == 0
    assert any(word in third.said.lower() for word in ("water", "drop", "unauthorized", "repair"))

    fourth = await conversation.say("How long should I bake sourdough bread?")
    assert fourth.finished.reference is NONE
    assert "forty" in fourth.said.lower() or "40" in fourth.said

    # Something said earlier is looked up, in the log, and quoted.
    await conversation.history.embed_pending(batch=16)
    fifth = await conversation.say("What did you tell me before about the router?")
    assert fifth.finished.reference is EARLIER
    assert fifth.finished.exchanges_used >= 1
    assert any(
        word in fifth.said.lower() for word in ("reset", "button", "ten seconds", "pulsante")
    )


NOTHING_TO_GO_ON = {
    "en": "I found no skill that fits, and no relevant document.",
    "it": "Non ho trovato una skill adatta, né documenti pertinenti.",
}


async def test_what_nothing_answers_is_declined_by_the_code_with_no_model_to_ask(
    conversation: Conversation,
) -> None:
    """ADR 0028, the first line: what no document is about is under the floor of meaning, so the
    code says so (a model could not write this exact sentence) and the model is not asked."""
    asked = {
        "Tell me a joke.": "en",
        "Who wrote the Divine Comedy?": "en",
        "What is your name?": "en",
        "Chi ha scritto la Divina Commedia?": "it",
        "Dimmi una barzelletta.": "it",
        "Come ti chiami?": "it",
        "Ciao!": "it",
    }
    for message, language in asked.items():
        talk = await conversation.say(message)
        assert talk.said == NOTHING_TO_GO_ON[language], message
        assert talk.finished.passages_used == 0
        assert talk.finished.usage is None  # nothing was written by a model


async def test_what_the_floor_lets_through_and_the_sources_do_not_answer_is_not_answered_from_them(
    conversation: Conversation,
) -> None:
    """The second line: a passage that is near (an oven in the recipe, the router in its manual) and
    does not answer. The model is asked, and says it cannot, or that it did not find it, instead of
    reading out the recipe or the way to erase the settings of a router that was to be restarted."""
    for message, forbidden in (
        ("Turn on the oven.", ("forty", "40 minutes", "starter")),
        ("Restart the router.", ("ten seconds", "factory", "reset button")),
        ("How do I make a pizza?", ("forty", "40 minutes", "starter")),
    ):
        talk = await conversation.say(message)
        said = talk.said.lower()
        print(f"    {talk.said.strip()!r}")
        assert not any(word in said for word in forbidden), (message, talk.said)
        # (A pizza is far enough from a loaf for the floor, and then it is the code that says so.)
        said_no = ("cannot", "can't", "did not find", "not find", "no relevant document")
        assert any(word in said for word in said_no), (message, talk.said)


async def test_what_was_said_is_found_by_meaning_in_another_language(
    conversation: Conversation,
) -> None:
    await conversation.history.embed_pending(batch=16)

    # Not a word of it is in what was said: only its meaning can find it.
    found = await conversation.service.search_history(
        PRINCIPAL, HistoryQuery(query="come si riportano le impostazioni alle condizioni originali")
    )

    print("\n  by meaning, in another language:")
    for hit in found.hits:
        print(f"    {hit.score:.2f} {hit.matched_by} {hit.exchange.question!r}")
    assert any(
        "router" in hit.exchange.question.lower() and hit.matched_by == ("vector",)
        for hit in found.hits
    )
