"""The actions service against real models: whether a message is a request to do something, which
skill it asks for, on which device and with which values; how a yes or a no is read; and how near,
by meaning, commands are to the skills they ask for, and questions are not.

Opt-in, because it needs a running Ollama with the models pulled:

    FACTOTUM_LIVE_TESTS=1 uv run pytest -m live services/actions/tests/live -s

``-s`` shows what was measured. Which models to use can be changed:

* FACTOTUM_LIVE_OLLAMA_URL: where Ollama is (http://127.0.0.1:11434).
* FACTOTUM_LIVE_FAST_MODEL: the model that reads the messages (llama3.1:8b-instruct-q4_K_M).
* FACTOTUM_LIVE_PLAN_MODELS: the models to measure, separated by commas (the one above).
* FACTOTUM_LIVE_EMBED_MODEL: the embedding model (bge-m3).

The messages are those of ``factotum.actions.plan_cases``, said to the devices of the example home
about the example skills, which are installed and enabled through the catalog as they are, and the
skills of the platform. What a model made of each is judged by ``factotum.actions.measure``: the
skill it chose and the values that skill was called with, whatever the device then did. The device
of the test answers as a plausible one does (``a_device_answers``) and what it did is not what is
measured here.

The messages of ``factotum.actions.mock_cases`` are about skills that the planner was never shown
anything like (``skills/mocks``), which do nothing and answer as their ``mock.yaml`` says: they are
measured with those skills beside the examples and the platform's, and the corpus above is measured
again with the mocks beside it, since a catalog with more skills in it is a harder one.
"""

import os
import statistics
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from factotum.actions.catalog import Catalog
from factotum.actions.confirmations import Answer, ConfirmationReader
from factotum.actions.core import platform_skills
from factotum.actions.matching import SkillMatcher
from factotum.actions.measure import Judged, measure
from factotum.actions.mock_cases import MOCK_CASES
from factotum.actions.mock_runner import MockRunner
from factotum.actions.plan_cases import DISPLAY, KITCHEN, PLAN_CASES, PlanCase
from factotum.actions.rig import ActionsRig
from factotum.actions.skills import load_folder
from factotum.actions.testing import FakeSpeech, a_device_answers
from factotum.common.config import Environment, ServiceSettings
from factotum.common.devauth import DEV_TENANT_ID, DEV_USER_ID, DevAuthenticator
from factotum.common.urlguard import UrlPolicy
from factotum.contracts.actions import ActStatus
from factotum.contracts.identity import Owner
from factotum.contracts.models import BindingOptions, ModelRole, ProviderKind
from factotum.contracts.testing import make_model_binding
from factotum.model_client import DevDelegation, GatewayChat, GatewayEmbedder
from factotum.model_gateway.api import create_app as create_gateway_app
from factotum.model_gateway.bindings import InMemoryBindingRepository
from factotum.model_gateway.core import ModelGateway, guarded_clients
from factotum.model_gateway.secrets import EnvSecretResolver

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("FACTOTUM_LIVE_TESTS") != "1",
        reason="live tests are opt-in: set FACTOTUM_LIVE_TESTS=1 (and run Ollama)",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

URL = os.environ.get("FACTOTUM_LIVE_OLLAMA_URL", "http://127.0.0.1:11434")
FAST_MODEL = os.environ.get("FACTOTUM_LIVE_FAST_MODEL", "llama3.1:8b-instruct-q4_K_M")
EMBED_MODEL = os.environ.get("FACTOTUM_LIVE_EMBED_MODEL", "bge-m3")
MODELS = [
    name.strip()
    for name in os.environ.get("FACTOTUM_LIVE_PLAN_MODELS", FAST_MODEL).split(",")
    if name.strip()
]

PATIENCE_S = 120.0
"""How long a model is waited for. What is measured is what a model makes of a message, not
whether a graphics card that is busy with something else answers in four seconds."""

NUM_CTX = 8704
"""The window that the conversation service's live tests use for its models too, so that a model
that answers there is the one that is already loaded here."""

ROOT = Path(__file__).resolve().parents[4]
HOME_TEXT = (ROOT / "config" / "devices.example.yaml").read_text("utf-8")
SKILLS = ROOT / "skills"
EXAMPLES = SKILLS / "examples"

OWNER = Owner.model_validate({"kind": "tenant", "id": str(DEV_TENANT_ID)})


@dataclass
class Gateway:
    """A gateway in this process, over real Ollama, and the clients that reach it."""

    chat: GatewayChat
    embedder: GatewayEmbedder
    http: httpx.AsyncClient
    gateway: ModelGateway


async def open_gateway(fast: str) -> Gateway:
    settings = ServiceSettings(environment=Environment.TEST, _env_file=None)
    bindings = [
        make_model_binding(
            tenant_id=DEV_TENANT_ID,
            role=ModelRole.CHAT_FAST,
            provider=ProviderKind.OLLAMA,
            model=fast,
            base_url=URL,
            options=BindingOptions(
                timeout_s=300, max_input_tokens=8192, max_output_tokens=300, num_ctx=NUM_CTX
            ),
        ),
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
        InMemoryBindingRepository(bindings),
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


async def home_of(opened: Gateway) -> ActionsRig:
    """The example home with the example skills installed, enabled and indexed by meaning, and the
    skills of the platform that every tenant has (``announce`` is one of them, ADR 0026)."""
    core = platform_skills(FakeSpeech())
    rig = ActionsRig(
        home_text=HOME_TEXT,
        chat=opened.chat,
        embedder=opened.embedder,
        patience_s=PATIENCE_S,
        core=core,
    )
    rig.dispatcher.responder = a_device_answers  # a device says how it is, and what it set
    catalog = Catalog(
        rig.store, rig.index, folder=EXAMPLES, enable_folder=True, core=core, now=rig.clock
    )
    read = await catalog.reload(rig.principal(None))
    assert read.problems == ()
    assert len(read.enabled) == 6  # the folder: the platform's skills are not in it
    return rig


async def everything_of(opened: Gateway) -> ActionsRig:
    """The example home with every skill of the folder of skills (the examples and the mocks)
    installed, enabled and indexed by meaning, and the skills of the platform: what a household
    that tried them all would have. A skill that runs is answered by its ``mock.yaml``."""
    core = platform_skills(FakeSpeech())
    rig = ActionsRig(
        home_text=HOME_TEXT,
        chat=opened.chat,
        embedder=opened.embedder,
        patience_s=PATIENCE_S,
        core=core,
    )
    rig.dispatcher.responder = a_device_answers
    rig.use_runner(MockRunner(rig.store))
    catalog = Catalog(
        rig.store, rig.index, folder=SKILLS, enable_folder=True, core=core, now=rig.clock
    )
    read = await catalog.reload(rig.principal(None))
    assert read.problems == ()
    assert len(read.enabled) == len(load_folder(SKILLS)[0])
    return rig


def percentile(values: Sequence[float], share: float) -> float:
    return sorted(values)[min(len(values) - 1, int(len(values) * share))]


async def run_corpus(rig: ActionsRig, cases: Sequence[PlanCase] = PLAN_CASES) -> list[Judged]:
    return [await measure(rig, case) for case in cases]


def describe(model: str, judged: Sequence[Judged]) -> tuple[float, float, float]:
    """Says what was measured, and gives the shares of questions left as questions, of commands
    with the right skill and of commands exactly as meant."""
    commands = [j for j in judged if j.case.skill is not None]
    questions = [j for j in judged if j.case.skill is None]
    print(f"\n  planner model {model}: {len(judged)} messages, embeddings by {EMBED_MODEL}")
    right_commands = sum(1 for j in commands if j.right)
    right_skill = sum(1 for j in commands if j.outcome.skill == j.case.skill)
    right_questions = sum(1 for j in questions if j.right)
    print(
        f"  commands: {right_commands}/{len(commands)} exactly as expected, "
        f"{right_skill}/{len(commands)} with the right skill"
    )
    print(f"  questions: {right_questions}/{len(questions)} left as questions")
    by_skill: dict[str, tuple[int, int]] = {}
    for j in commands:
        name = j.case.skill or ""
        right, total = by_skill.get(name, (0, 0))
        by_skill[name] = (right + int(j.right), total + 1)
    print("  by skill: " + ", ".join(f"{n} {r}/{t}" for n, (r, t) in sorted(by_skill.items())))
    asked_the_model = [j.seconds for j in judged if j.outcome.status is not ActStatus.NO_SKILL]
    passed_over = [j.seconds for j in questions if j.right]
    print(
        f"  a message that is acted on takes {statistics.median(asked_the_model) * 1000:.0f} ms "
        f"(median), {percentile(asked_the_model, 0.9) * 1000:.0f} ms (90th percentile); "
        f"a question that is passed over takes {statistics.median(passed_over) * 1000:.0f} ms"
    )
    for j in judged:
        if not j.right:
            print(f"  wrong: {j.case.message!r} at {j.case.speaker}: {'; '.join(j.problems)}")
    return (
        right_questions / len(questions),
        right_skill / len(commands),
        right_commands / len(commands),
    )


def nothing_that_was_refused_was_done(judged: Sequence[Judged]) -> None:
    """What must hold whatever the model: what the policy refuses is never done, whatever it
    read, and what asks for a yes does not run before it."""
    for j in judged:
        if j.case.status in {ActStatus.REFUSED, ActStatus.NEEDS_CONFIRMATION}:
            assert j.outcome.status is not ActStatus.DONE, j.case.message


@pytest.mark.parametrize("model", MODELS)
async def test_a_small_model_tells_commands_from_questions_and_reads_them(model: str) -> None:
    opened = await open_gateway(model)
    try:
        rig = await home_of(opened)
        # The first call loads the model: it is not what a message pays for, so it is not measured.
        await rig.hear(KITCHEN, "warm up the model")
        rig.runner.requests.clear()
        judged = await run_corpus(rig)
    finally:
        await close_gateway(opened)

    questions, skills, exactly = describe(model, judged)

    nothing_that_was_refused_was_done(judged)
    # What must hold of a model worth binding to the fast role: a question is not taken for a
    # command (that is the one mistake that does something nobody asked for), and most commands are
    # read as they were meant.
    assert questions >= 0.85
    assert skills >= 0.85
    assert exactly >= 0.75


@pytest.mark.parametrize("model", MODELS)
async def test_a_small_model_chooses_and_calls_skills_it_was_never_shown(model: str) -> None:
    """The mocks are written plainly and nothing the planner is shown is about them: this is how
    it does with a skill that somebody wrote. The floors are low, for a first measurement: the
    numbers are what this is for."""
    opened = await open_gateway(model)
    try:
        rig = await everything_of(opened)
        await rig.hear(KITCHEN, "warm up the model")
        judged = await run_corpus(rig, MOCK_CASES)
    finally:
        await close_gateway(opened)

    questions, skills, exactly = describe(model, judged)

    nothing_that_was_refused_was_done(judged)
    assert questions >= 0.7
    assert skills >= 0.7
    assert exactly >= 0.5


@pytest.mark.parametrize("model", MODELS)
async def test_the_corpus_of_the_examples_holds_with_the_mocks_beside_them(model: str) -> None:
    """A catalog with more skills in it is a harder one: the skills that the planner was shown how
    to read are asked about again with the mocks beside them, and the floors are those of the
    corpus without them."""
    opened = await open_gateway(model)
    try:
        rig = await everything_of(opened)
        await rig.hear(KITCHEN, "warm up the model")
        judged = await run_corpus(rig)
    finally:
        await close_gateway(opened)

    questions, skills, exactly = describe(model, judged)

    nothing_that_was_refused_was_done(judged)
    assert questions >= 0.8
    assert skills >= 0.8
    assert exactly >= 0.7


@pytest.mark.parametrize("model", MODELS)
async def test_commands_are_near_their_skills_by_meaning_and_questions_are_not(
    model: str,
) -> None:
    opened = await open_gateway(model)
    try:
        rig = await home_of(opened)
        matcher = SkillMatcher(
            rig.store, opened.embedder, rig.index, threshold=0.0, max_candidates=20
        )
        wanted: list[tuple[PlanCase, float]] = []
        unwanted: list[tuple[PlanCase, float]] = []
        for case in PLAN_CASES:
            found = await matcher.candidates(rig.principal(case.speaker), case.message)
            scores = {c.skill.name: c.score or 0.0 for c in found.found}
            if case.skill is None:
                unwanted.append((case, max(scores.values(), default=0.0)))
            else:
                wanted.append((case, scores.get(case.skill, 0.0)))
    finally:
        await close_gateway(opened)

    def stats(what: str, scores: list[tuple[PlanCase, float]]) -> None:
        values = [score for _, score in scores]
        print(
            f"  {what}: lowest {min(values):.2f}, median {statistics.median(values):.2f}, "
            f"highest {max(values):.2f}"
        )

    print(f"\n  embedding model {EMBED_MODEL}: the nearest that each message is to a skill")
    stats("commands, to the skill they ask for", wanted)
    stats("questions, to the nearest skill    ", unwanted)
    for threshold in (0.35, 0.40, 0.45, 0.50, 0.55, 0.60):
        passed = sum(1 for _, s in wanted if s >= threshold)
        let_through = sum(1 for _, s in unwanted if s >= threshold)
        print(
            f"  at {threshold:.2f}: {passed}/{len(wanted)} commands go on to the model, "
            f"{let_through}/{len(unwanted)} questions do too"
        )
    for case, score in sorted(wanted, key=lambda pair: pair[1])[:4]:
        print(f"  a command far from its skill: {score:.2f} {case.message!r}")
    for case, score in sorted(unwanted, key=lambda pair: -pair[1])[:4]:
        print(f"  a question near a skill: {score:.2f} {case.message!r}")

    # The threshold that ships must not lose commands: a command that is filtered out here never
    # reaches the model, whatever the model would have made of it.
    default = 0.45
    assert sum(1 for _, s in wanted if s >= default) / len(wanted) >= 0.95


REPLIES = (
    # (what the person said, what it means)
    ("sure, go ahead", Answer.YES),
    ("yes please", Answer.YES),
    ("absolutely", Answer.YES),
    ("do it", Answer.YES),
    ("sì, fai pure", Answer.YES),
    ("certo, apri pure", Answer.YES),
    ("va bene, procedi", Answer.YES),
    ("nope", Answer.NO),
    ("no, leave it", Answer.NO),
    ("better not", Answer.NO),
    ("cancel that", Answer.NO),
    ("no lascia stare", Answer.NO),
    ("meglio di no", Answer.NO),
    ("lascia perdere", Answer.NO),
    ("wait, which door?", Answer.OTHER),
    ("let me think about it", Answer.OTHER),
    ("what time is it?", Answer.OTHER),
    ("yes, and turn on the lamp too", Answer.OTHER),
    ("no, close it instead", Answer.OTHER),
    ("aspetta un attimo", Answer.OTHER),
    ("che tempo fa a Roma?", Answer.OTHER),
    ("sì ma prima spegni la luce", Answer.OTHER),
)


@pytest.mark.parametrize("model", MODELS)
async def test_a_yes_or_a_no_is_read_in_english_and_in_italian(model: str) -> None:
    opened = await open_gateway(model)
    try:
        reader = ConfirmationReader(opened.chat, timeout_s=PATIENCE_S)
        question = "Do you really want me to open the garage door?"
        await reader.read(OWNER, question, "warm up")
        read: list[tuple[str, Answer, Answer]] = []
        seconds: list[float] = []
        for said, meaning in REPLIES:
            began = time.perf_counter()
            answer = await reader.read(OWNER, question, said)
            seconds.append(time.perf_counter() - began)
            read.append((said, meaning, answer))
    finally:
        await close_gateway(opened)

    right = [(said, meant, got) for said, meant, got in read if meant is got]
    print(f"\n  reader model {model}: {len(right)}/{len(read)} answers read as they were meant")
    print(f"  time for one answer: median {statistics.median(seconds) * 1000:.0f} ms")
    for said, meant, got in read:
        if meant is not got:
            print(f"  wrong: {said!r} means {meant.value}, read as {got.value}")

    # The one mistake that matters: a yes that was not one. A reader that does that is not used.
    assert not [said for said, meant, got in read if got is Answer.YES and meant is not Answer.YES]
    assert len(right) / len(read) >= 0.8


async def test_a_confirmation_is_asked_and_a_yes_in_words_the_model_reads_goes_ahead() -> None:
    opened = await open_gateway(MODELS[0])
    try:
        rig = await home_of(opened)
        asked = await rig.hear(DISPLAY, "open the garage door")
        answered = await rig.hear(DISPLAY, "sure, go ahead")
    finally:
        await close_gateway(opened)

    print(f"\n  asked: {asked.reply!r}; answered: {answered.reply!r}")
    assert asked.status is ActStatus.NEEDS_CONFIRMATION
    assert answered.status is ActStatus.DONE
    assert [r.skill_name for r in rig.runner.requests] == ["garage-door"]
    trail = await rig.audit()
    assert [(r.confirmed, r.result.value) for r in trail] == [
        (None, "not_executed"),
        (True, "executed"),
    ]
