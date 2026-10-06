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
about the example skills, which are installed and enabled through the catalog as they are.
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
from factotum.actions.plan_cases import DISPLAY, KITCHEN, PLAN_CASES, PlanCase
from factotum.actions.rig import ActionsRig
from factotum.actions.testing import FakeSpeech
from factotum.common.config import Environment, ServiceSettings
from factotum.common.devauth import DEV_TENANT_ID, DEV_USER_ID, DevAuthenticator
from factotum.common.urlguard import UrlPolicy
from factotum.contracts.actions import ActOutcome, ActStatus
from factotum.contracts.commands import PlayAudio
from factotum.contracts.identity import Owner, Principal
from factotum.contracts.models import BindingOptions, ModelRole, ProviderKind
from factotum.contracts.runner import RunRequest
from factotum.contracts.skills import ParamValue
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
EXAMPLES = ROOT / "skills" / "examples"

ENTITIES = {
    "Living room lamp": "light.living_room",
    "Bedroom lamp": "light.bedroom",
    "Garage door": "cover.garage",
}
"""The entity each external device of the example home is bound to."""

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
    catalog = Catalog(
        rig.store, rig.index, folder=EXAMPLES, enable_folder=True, core=core, now=rig.clock
    )
    read = await catalog.reload(rig.principal(None))
    assert read.problems == ()
    assert len(read.enabled) == 7  # the folder: the platform's skills are not in it
    return rig


async def forget_what_is_asked(rig: ActionsRig, principal: Principal) -> None:
    """A yes or a no that was asked for is answered by the next message on that device: what is
    measured here is one message at a time."""
    pending = await rig.store.get_pending(rig.tenant, principal.acts_as, principal.device_id)
    if pending is not None:
        await rig.store.take_pending(rig.tenant, pending.id)


@dataclass
class Judged:
    case: PlanCase
    outcome: ActOutcome
    seconds: float
    ran: RunRequest | None
    problems: list[str]

    @property
    def right(self) -> bool:
        return not self.problems


def same(understood: object, wanted: ParamValue) -> bool:
    """What a person said is not what a model has to repeat letter for letter: a text is the same
    whatever its case and whatever punctuation it ends with."""
    if isinstance(understood, str) and isinstance(wanted, str):
        return understood.strip().strip(".!?").casefold() == wanted.strip().strip(".!?").casefold()
    return understood == wanted


def judge(
    case: PlanCase, outcome: ActOutcome, ran: RunRequest | None, said: Sequence[str] = ()
) -> list[str]:
    """What is wrong with what became of a message, in a few words each; nothing when it is
    right. ``said`` is the words of the clips that a device was given: a skill of the platform
    (``announce``) is not run by the runner, it gives clips."""
    problems: list[str] = []
    if outcome.status is not case.status:
        problems.append(f"{outcome.status.value} instead of {case.status.value}")
    if outcome.skill != case.skill:
        problems.append(f"skill {outcome.skill} instead of {case.skill}")
    if case.skill == "announce":
        if case.status is ActStatus.DONE:
            if not said:
                problems.append("nothing was said")
            elif not same(" ".join(said), case.params["message"]):
                problems.append(
                    f"{' '.join(said)!r} was said instead of {case.params['message']!r}"
                )
    elif ran is not None:
        for name, value in case.params.items():
            if not same(ran.params.get(name), value):
                problems.append(f"{name}={ran.params.get(name)!r} instead of {value!r}")
        if case.device in ENTITIES and ran.device_params.get("entity_id") != ENTITIES[case.device]:
            problems.append(f"device {ran.device_params.get('entity_id')} instead of {case.device}")
    elif case.status is ActStatus.DONE:
        problems.append("nothing was run")
    return problems


def percentile(values: Sequence[float], share: float) -> float:
    return sorted(values)[min(len(values) - 1, int(len(values) * share))]


async def run_corpus(rig: ActionsRig) -> list[Judged]:
    judged: list[Judged] = []
    for case in PLAN_CASES:
        principal = rig.principal(case.speaker)
        await forget_what_is_asked(rig, principal)
        before = len(rig.runner.requests)
        given = len(rig.dispatcher.given)
        began = time.perf_counter()
        outcome = await rig.hear(case.speaker, case.message)
        seconds = time.perf_counter() - began
        ran = rig.runner.requests[-1] if len(rig.runner.requests) > before else None
        said = [c.text for _, _, c in rig.dispatcher.given[given:] if isinstance(c, PlayAudio)]
        judged.append(Judged(case, outcome, seconds, ran, judge(case, outcome, ran, said)))
    return judged


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
    asked_the_model = [j.seconds for j in judged if j.outcome.status is not ActStatus.NO_ACTION]
    passed_over = [j.seconds for j in questions if j.right]
    print(
        f"  a message that is acted on takes {statistics.median(asked_the_model) * 1000:.0f} ms "
        f"(median), {percentile(asked_the_model, 0.9) * 1000:.0f} ms (90th percentile); "
        f"a question that is passed over takes {statistics.median(passed_over) * 1000:.0f} ms"
    )
    for j in judged:
        if not j.right:
            print(f"  wrong: {j.case.message!r} at {j.case.speaker}: {'; '.join(j.problems)}")

    # What must hold whatever the model: what the policy refuses is never done, whatever it read.
    for j in judged:
        if j.case.status in {ActStatus.REFUSED, ActStatus.NEEDS_CONFIRMATION}:
            assert j.outcome.status is not ActStatus.DONE, j.case.message
    # What must hold of a model worth binding to the fast role: a question is not taken for a
    # command (that is the one mistake that does something nobody asked for), and most commands are
    # read as they were meant.
    assert right_questions / len(questions) >= 0.85
    assert right_skill / len(commands) >= 0.85
    assert right_commands / len(commands) >= 0.75


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
