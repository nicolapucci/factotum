"""Skills that are instructions, against real models: does a model write plans that the checks
accept and that do what the skill says, and does a message come to that through both models.

Opt-in, because it needs a running Ollama with the models pulled:

    FACTOTUM_LIVE_TESTS=1 uv run pytest -m live services/actions/tests/live -s -k live_instructions

``-s`` shows what was measured. Which models to use can be changed:

* FACTOTUM_LIVE_OLLAMA_URL: where Ollama is (http://127.0.0.1:11434).
* FACTOTUM_LIVE_FAST_MODEL: the model that reads the messages (llama3.1:8b-instruct-q4_K_M).
* FACTOTUM_LIVE_WRITER_MODELS: the models that write the plans, separated by commas: the ones to
  measure (the one above).
* FACTOTUM_LIVE_EMBED_MODEL: the embedding model (bge-m3).

*Right* is what a web server that this test runs was asked, and what a device was told to say: a
plan is not judged by how it reads but by what it does, and it is run by the program that the
sandbox runs. The skills are the ones that come with the project, and five more that are written
here and that nothing was tuned on (they say how a model does with a skill it has not been shown
anything like).
"""

import json
import os
import statistics
import time
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from factotum.actions.catalog import Catalog
from factotum.actions.instructions.checks import check_plan
from factotum.actions.instructions.executor import Context, StepError, run
from factotum.actions.instructions.testing import FakeWeb
from factotum.actions.instructions.writer import PlanInputs, PlanWriter
from factotum.actions.rig import ActionsRig
from factotum.actions.runner_client import hosts_of
from factotum.actions.skills import LoadedSkill, load_folder
from factotum.common.config import Environment, ServiceSettings
from factotum.common.devauth import DEV_TENANT_ID, DEV_USER_ID, DevAuthenticator
from factotum.common.urlguard import UrlPolicy
from factotum.contracts.actions import ActStatus
from factotum.contracts.chat import ChatRequest, ChatResult, EventUnion
from factotum.contracts.errors import FactotumError
from factotum.contracts.identity import Owner
from factotum.contracts.models import BindingOptions, ModelRole, Profile, ProviderKind
from factotum.contracts.skills import ParamValue
from factotum.contracts.testing import make_model_binding
from factotum.model_client import Chat, DevDelegation, GatewayChat, GatewayEmbedder
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
WRITER_MODELS = [
    name.strip()
    for name in os.environ.get("FACTOTUM_LIVE_WRITER_MODELS", FAST_MODEL).split(",")
    if name.strip()
]

PATIENCE_S = 180.0
"""How long a model is waited for: what is measured is what a model writes, not whether a graphics
card that is busy with something else answers in four seconds."""

NUM_CTX = 8704
"""The window that the other live tests use for their models too, so that a model that answers there
is the one that is already loaded here."""

ROOT = Path(__file__).resolve().parents[4]
HOME_TEXT = (ROOT / "config" / "devices.example.yaml").read_text("utf-8")
EXAMPLES = ROOT / "skills" / "examples"

OWNER = Owner.model_validate({"kind": "tenant", "id": str(DEV_TENANT_ID)})
KITCHEN = "Kitchen speaker"
DISPLAY = "Living room display"
TOKEN = "the-token-of-the-home"
TOPIC = "the-topic-of-the-home"
TRACK_KEY = "the-key-of-the-tracking-service"

# ---- five skills that nothing was tuned on ----------------------------------------------------

THERMOSTAT = """\
---
name: thermostat
description: Set the temperature of a room's thermostat.
examples:
  - set the living room to 21 degrees
  - make the bedroom warmer, 22 degrees
  - metti il salotto a 20 gradi
target: external
parameters:
  temperature: {type: number, minimum: 5, maximum: 30, description: "the temperature, in degrees"}
required: [temperature]
device_parameters:
  entity_id: {type: string, description: the climate entity of the room in Home Assistant}
  host: {type: string, description: the host name of Home Assistant}
permissions:
  network: ["${device.host}"]
  secrets: [HA_TOKEN]
effect: reversible
---
The thermostat is a `climate` entity of Home Assistant. Set its target temperature: POST
`http://HOST:8123/api/services/climate/set_temperature` with the JSON body
`{"entity_id": ENTITY, "temperature": DEGREES}` and the header `Authorization: Bearer TOKEN`.

Say the temperature that was set, in the language of the message.
"""

NOTIFY = """\
---
name: notify
description: Send a notification to the phones of the household.
examples:
  - send a notification that the laundry is done
  - notify everyone that dinner is ready
  - manda una notifica, il bucato è pronto
target: none
parameters:
  message: {type: string, description: "the text of the notification, as the person said it"}
required: [message]
permissions:
  network: [ntfy.sh]
  secrets: [NTFY_TOPIC]
effect: reversible
---
Notifications go through ntfy. POST `https://ntfy.sh/TOPIC` with the message itself as the body (a
string, not JSON) and the header `Title: Home`. The topic is the secret `NTFY_TOPIC`.

Say that the notification was sent, in the language of the message.
"""

GARAGE_STATE = """\
---
name: garage-state
description: Say whether the garage door is open or closed.
examples:
  - is the garage door open
  - did I close the garage
  - il garage è aperto
target: external
device_parameters:
  entity_id: {type: string, description: the entity of the door in Home Assistant}
  host: {type: string, description: the host name of Home Assistant}
permissions:
  network: ["${device.host}"]
  secrets: [HA_TOKEN]
effect: read_only
---
Read the state of the door: GET `http://HOST:8123/api/states/ENTITY` with the header
`Authorization: Bearer TOKEN`. The answer has `state`, which is `open` or `closed`.

Say whether the door is open or closed, in the language of the message.
"""

COFFEE = """\
---
name: coffee
description: Switch the coffee machine on and say so.
examples:
  - make some coffee
  - switch on the coffee machine
  - prepara il caffè
target: none
permissions:
  network: [homeassistant.local]
  secrets: [HA_TOKEN]
effect: reversible
---
1. Switch the coffee machine on: POST `http://homeassistant.local:8123/api/services/switch/turn_on`
   with `{"entity_id": "switch.coffee_machine"}` and the header `Authorization: Bearer TOKEN`.
2. Have the device say "The coffee machine is on": one `speak` command with that text.
"""

PARCEL = """\
---
name: parcel
description: Say where a parcel is, by its tracking number.
examples:
  - where is my parcel
  - track the parcel RR123456789IT
  - dov'è il mio pacco
target: none
parameters:
  number: {type: string, description: "the tracking number, as the person said it"}
required: [number]
permissions:
  network: [track.example.org]
  secrets: [TRACK_KEY]
effect: read_only
---
Ask the tracking service: GET `https://track.example.org/v2/parcels/NUMBER` with the header
`X-Api-Key: KEY`. The key is the secret `TRACK_KEY`. The answer has `status`, where the parcel is
(`in transit`, `delivered`...).

Say where the parcel is, in the language of the message.
"""

HELD_OUT = {
    "thermostat": THERMOSTAT,
    "notify": NOTIFY,
    "garage-state": GARAGE_STATE,
    "coffee": COFFEE,
    "parcel": PARCEL,
}


# ---- what each skill is judged by -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Case:
    skill: str
    values: Mapping[str, ParamValue]
    language: str
    device: Mapping[str, ParamValue] = field(default_factory=dict)
    """What is fixed for the device that the skill acts on."""


THERMOSTAT_OF = {"host": "ha.local", "entity_id": "climate.living_room"}
DOOR_OF = {"host": "ha.local", "entity_id": "cover.garage"}

CASES = (
    Case("announce", {"message": "Dinner is ready"}, "en"),
    Case("announce", {"message": "the guests have arrived"}, "en"),
    Case("announce", {"message": "la cena è pronta, venite a tavola"}, "it"),
    Case("announce", {"message": 'Tom said "come here" \U0001f600'}, "en"),
    Case("movie-scene", {}, "en"),
    Case("movie-scene", {}, "it"),
    Case("air-quality", {"place": "Rome"}, "en"),
    Case("air-quality", {"place": "Torino"}, "it"),
    Case("air-quality", {"place": "Milan"}, "en"),
    Case("air-quality", {"place": "New York"}, "en"),
    Case("air-quality", {"place": "Reggio Emilia"}, "it"),
    Case("air-quality", {"place": "San José"}, "en"),
    Case("thermostat", {"temperature": 21}, "en", THERMOSTAT_OF),
    Case("thermostat", {"temperature": 19.5}, "it", THERMOSTAT_OF),
    Case("notify", {"message": "the laundry is done"}, "en"),
    Case("notify", {"message": "il bucato è pronto"}, "it"),
    Case("garage-state", {}, "en", DOOR_OF),
    Case("garage-state", {}, "it", DOOR_OF),
    Case("coffee", {}, "en"),
    Case("coffee", {}, "it"),
    Case("parcel", {"number": "RR123456789IT"}, "en"),
    Case("parcel", {"number": "AB987654321XY"}, "it"),
)

SHIPPED = {"announce", "movie-scene", "air-quality"}

Answer = dict[str, Any]
"""What the program that runs a plan answers: the ``message`` to say, the ``commands`` to give."""


def calls_of(web: FakeWeb) -> list[tuple[str, str]]:
    return [(seen.method, seen.path) for seen in web.seen]


def serve(web: FakeWeb) -> None:
    """What the home and the web answer, whichever skill asks."""
    web.answer("POST", "/api/services/light/turn_off", json_body=[])
    web.answer("POST", "/api/services/light/turn_on", json_body=[])
    web.answer("POST", "/api/services/climate/set_temperature", json_body=[])
    web.answer("POST", "/api/services/switch/turn_on", json_body=[])
    web.answer("POST", f"/{TOPIC}", text="ok")
    web.answer("GET", "/api/states/cover.garage", json_body={"state": "open", "attributes": {}})
    for number in ("RR123456789IT", "AB987654321XY"):
        web.answer("GET", f"/v2/parcels/{number}", json_body={"status": "in transit"})
    web.answer(
        "GET",
        "/v1/search",
        json_body={"results": [{"name": "X", "latitude": 41.89193, "longitude": 12.51133}]},
    )
    web.answer(
        "GET",
        "/v1/air-quality",
        json_body={"current": {"time": "2026-09-25T10:00", "european_aqi": 33}},
    )


def spoken(answer: Answer) -> list[str]:
    """What a device is told to say, and what is said back: both are ways to say it."""
    said = [c.get("text", "") for c in answer["commands"] if c.get("name") == "speak"]
    return [*said, answer.get("message") or ""]


def wrong_announce(case: Case, answer: Answer, web: FakeWeb) -> list[str]:
    problems = []
    if answer["commands"] != [{"name": "speak", "text": case.values["message"]}]:
        problems.append(f"it gave {answer['commands']}")
    if web.seen:
        problems.append("it reached the web")
    return problems


def wrong_movie_scene(case: Case, answer: Answer, web: FakeWeb) -> list[str]:
    turn_off, turn_on = (
        ("POST", "/api/services/light/turn_off"),
        ("POST", "/api/services/light/turn_on"),
    )
    if calls_of(web) != [turn_off, turn_on]:
        return [f"it made the calls {calls_of(web)}"]
    problems = []
    if web.seen[0].json() != {"entity_id": "light.living_room_ceiling"}:
        problems.append(f"the first body is {web.seen[0].json()}")
    if web.seen[1].json() != {"entity_id": "light.tv_backlight", "brightness_pct": 20}:
        problems.append(f"the second body is {web.seen[1].json()}")
    if {seen.headers.get("authorization") for seen in web.seen} != {f"Bearer {TOKEN}"}:
        problems.append("it did not send the token")
    if not answer.get("message"):
        problems.append("it said nothing")
    return problems


def wrong_air_quality(case: Case, answer: Answer, web: FakeWeb) -> list[str]:
    if calls_of(web) != [("GET", "/v1/search"), ("GET", "/v1/air-quality")]:
        return [f"it made the calls {calls_of(web)}"]
    problems = []
    if parse_qs(web.seen[0].query).get("name") != [case.values["place"]]:
        problems.append(f"it looked up {parse_qs(web.seen[0].query).get('name')}")
    air = parse_qs(web.seen[1].query)
    if air.get("latitude") != ["41.89193"] or air.get("longitude") != ["12.51133"]:
        problems.append(f"it asked about {air.get('latitude')}, {air.get('longitude')}")
    if air.get("current") != ["european_aqi"]:
        problems.append(f"it asked for {air.get('current')}")
    said = answer.get("message") or ""
    if "33" not in said:
        problems.append(f"it did not say the index: {said!r}")
    if str(case.values["place"]) not in said:
        problems.append(f"it did not say the place: {said!r}")
    return problems


def wrong_thermostat(case: Case, answer: Answer, web: FakeWeb) -> list[str]:
    if calls_of(web) != [("POST", "/api/services/climate/set_temperature")]:
        return [f"it made the calls {calls_of(web)}"]
    problems = []
    body = web.seen[0].json()
    wanted = {"entity_id": "climate.living_room", "temperature": case.values["temperature"]}
    if body != wanted or isinstance(body.get("temperature"), str):
        problems.append(f"the body is {body}")
    if web.seen[0].headers.get("authorization") != f"Bearer {TOKEN}":
        problems.append("it did not send the token")
    said = (answer.get("message") or "").replace(",", ".")
    if str(case.values["temperature"]) not in said:
        problems.append(f"it did not say the temperature: {answer.get('message')!r}")
    return problems


def wrong_notify(case: Case, answer: Answer, web: FakeWeb) -> list[str]:
    if calls_of(web) != [("POST", f"/{TOPIC}")]:
        return [f"it made the calls {calls_of(web)}"]
    problems = []
    if web.seen[0].body.decode() != case.values["message"]:
        problems.append(f"the body is {web.seen[0].body!r}")
    if web.seen[0].headers.get("title") != "Home":
        problems.append(f"the title is {web.seen[0].headers.get('title')!r}")
    if not answer.get("message"):
        problems.append("it said nothing")
    return problems


def wrong_garage_state(case: Case, answer: Answer, web: FakeWeb) -> list[str]:
    if calls_of(web) != [("GET", "/api/states/cover.garage")]:
        return [f"it made the calls {calls_of(web)}"]
    problems = []
    if web.seen[0].headers.get("authorization") != f"Bearer {TOKEN}":
        problems.append("it did not send the token")
    if not any(word in (answer.get("message") or "").lower() for word in ("open", "apert")):
        problems.append(f"it did not say the door is open: {answer.get('message')!r}")
    return problems


def wrong_parcel(case: Case, answer: Answer, web: FakeWeb) -> list[str]:
    if calls_of(web) != [("GET", f"/v2/parcels/{case.values['number']}")]:
        return [f"it made the calls {calls_of(web)}"]
    problems = []
    if web.seen[0].headers.get("x-api-key") != TRACK_KEY:
        problems.append("it did not send the key")
    said = (answer.get("message") or "").lower()
    if "transit" not in said and "transito" not in said:
        problems.append(f"it did not say where the parcel is: {answer.get('message')!r}")
    return problems


def wrong_coffee(case: Case, answer: Answer, web: FakeWeb) -> list[str]:
    problems = []
    if calls_of(web) != [("POST", "/api/services/switch/turn_on")]:
        return [f"it made the calls {calls_of(web)}"]
    if web.seen[0].json() != {"entity_id": "switch.coffee_machine"}:
        problems.append(f"the body is {web.seen[0].json()}")
    if not any(word in text.lower() for text in spoken(answer) for word in ("coffee", "caff")):
        problems.append(f"it did not say it: {spoken(answer)}")
    return problems


JUDGES: dict[str, Callable[[Case, Answer, FakeWeb], list[str]]] = {
    "announce": wrong_announce,
    "movie-scene": wrong_movie_scene,
    "air-quality": wrong_air_quality,
    "thermostat": wrong_thermostat,
    "notify": wrong_notify,
    "garage-state": wrong_garage_state,
    "coffee": wrong_coffee,
    "parcel": wrong_parcel,
}


# ---- a gateway over real Ollama ---------------------------------------------------------------


@dataclass
class Gateway:
    chat: GatewayChat
    embedder: GatewayEmbedder
    http: httpx.AsyncClient
    gateway: ModelGateway


async def open_gateway(main: str) -> Gateway:
    """The model that reads a message, the one that writes a plan and the one that embeds, bound as
    a home would bind them."""
    settings = ServiceSettings(environment=Environment.TEST, _env_file=None)

    def binding(role: ModelRole, model: str, options: BindingOptions) -> Any:
        return make_model_binding(
            tenant_id=DEV_TENANT_ID,
            role=role,
            provider=ProviderKind.OLLAMA,
            model=model,
            base_url=URL,
            options=options,
        )

    gateway = ModelGateway(
        InMemoryBindingRepository(
            [
                binding(
                    ModelRole.CHAT_FAST,
                    FAST_MODEL,
                    BindingOptions(
                        timeout_s=300, max_input_tokens=8192, max_output_tokens=300, num_ctx=NUM_CTX
                    ),
                ),
                binding(
                    ModelRole.CHAT_MAIN,
                    main,
                    BindingOptions(
                        timeout_s=300,
                        max_input_tokens=8192,
                        max_output_tokens=1000,
                        num_ctx=NUM_CTX,
                    ),
                ),
                binding(ModelRole.EMBEDDING, EMBED_MODEL, BindingOptions(timeout_s=300)),
            ]
        ),
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


class Counted:
    """A chat that counts how many times the model was asked, and for how long."""

    def __init__(self, inner: Chat) -> None:
        self._inner = inner
        self.seconds: list[float] = []

    async def chat(self, owner: Owner, request: ChatRequest) -> ChatResult:
        began = time.perf_counter()
        try:
            return await self._inner.chat(owner, request)
        finally:
            self.seconds.append(time.perf_counter() - began)

    def chat_stream(self, owner: Owner, request: ChatRequest) -> AsyncGenerator[EventUnion]:
        return self._inner.chat_stream(owner, request)

    async def profile(self, owner: Owner, role: ModelRole) -> Profile:
        return await self._inner.profile(owner, role)


# ---- the plan of each skill, from the writer to the sandbox's program -------------------------


def skills_by_name(folder: Path) -> dict[str, LoadedSkill]:
    for name, text in HELD_OUT.items():
        (folder / name).mkdir()
        (folder / name / "SKILL.md").write_text(text, encoding="utf-8")
    skills, problems = load_folder(folder)
    assert problems == []
    return {skill.manifest.name: skill for skill in skills}


@dataclass
class Judged:
    case: Case
    attempts: int
    seconds: float
    problems: list[str]
    refused: str | None = None
    """Why no plan was accepted, when none was."""

    @property
    def accepted(self) -> bool:
        return self.refused is None

    @property
    def right(self) -> bool:
        return self.accepted and not self.problems


def percentile(values: Sequence[float], share: float) -> float:
    return sorted(values)[min(len(values) - 1, int(len(values) * share))]


async def write_and_run(opened: Gateway, skill: LoadedSkill, case: Case) -> Judged:
    manifest = skill.manifest
    granted = manifest.permissions
    hosts = hosts_of(granted, case.device)
    chat = Counted(opened.chat)
    writer = PlanWriter(chat, timeout_s=PATIENCE_S)

    def check(plan: Any) -> None:
        check_plan(
            plan,
            manifest=manifest,
            granted=granted,
            hosts=hosts,
            values=case.values,
            device_params=case.device,
        )

    began = time.perf_counter()
    try:
        plan = await writer.write(
            OWNER,
            PlanInputs(
                name=manifest.name,
                description=manifest.description,
                instructions=skill.instructions,
                effect=manifest.effect,
                values=case.values,
                device_values=tuple(manifest.device_parameters),
                hosts=hosts,
                secrets=granted.secrets,
                language=case.language,
            ),
            check,
        )
    except FactotumError as error:
        # No plan that the checks accept, or a model that did not answer with a plan at all (it
        # went on until it was cut off): either way the person hears that it did not work.
        return Judged(case, len(chat.seconds), time.perf_counter() - began, [], refused=str(error))
    seconds = time.perf_counter() - began

    with FakeWeb() as web:
        serve(web)
        written = plan.model_dump_json()
        for host in hosts:
            written = written.replace(f"https://{host}", web.url("")).replace(
                f"http://{host}:8123", web.url("")
            )
        written = written.replace("http://${device.host}:8123", web.url(""))
        written = written.replace("${secret.NTFY_TOPIC}", TOPIC)
        try:
            answer = run(
                json.loads(written),
                context=Context(
                    secrets={"HA_TOKEN": TOKEN, "NTFY_TOPIC": TOPIC, "TRACK_KEY": TRACK_KEY},
                    device=case.device,
                    values=case.values,
                ),
                budget_s=30,
            )
        except StepError as error:
            # A plan that the checks accept can still fail when it is run: it asked for something
            # that is not there. That is a plan that did not do what the skill says.
            return Judged(case, len(chat.seconds), seconds, [f"the run failed: step {error}"])
        problems = JUDGES[case.skill](case, answer, web)
    return Judged(case, len(chat.seconds), seconds, problems)


@pytest.mark.parametrize("model", WRITER_MODELS)
async def test_a_model_writes_plans_that_the_checks_accept_and_that_do_what_the_skill_says(
    model: str, tmp_path: Path
) -> None:
    opened = await open_gateway(model)
    try:
        skills = skills_by_name(tmp_path)
        skills.update({s.manifest.name: s for s in load_folder(EXAMPLES)[0]})
        # The first call loads the model: it is not what a plan pays for, so it is not measured.
        await write_and_run(opened, skills["announce"], CASES[0])
        judged = [await write_and_run(opened, skills[case.skill], case) for case in CASES]
    finally:
        await close_gateway(opened)

    def share(what: str, of: Sequence[Judged]) -> None:
        print(
            f"  {what}: {sum(1 for j in of if j.right)}/{len(of)} do what the skill says, "
            f"{sum(1 for j in of if j.accepted)}/{len(of)} accepted, "
            f"{sum(1 for j in of if j.accepted and j.attempts == 1)}/{len(of)} at the first try"
        )

    print(f"\n  plan writer {model}: {len(judged)} plans, embeddings and reading by {FAST_MODEL}")
    share("the skills that come with the project", [j for j in judged if j.case.skill in SHIPPED])
    share("skills that nothing was tuned on   ", [j for j in judged if j.case.skill not in SHIPPED])
    by_skill: dict[str, list[Judged]] = {}
    for j in judged:
        by_skill.setdefault(j.case.skill, []).append(j)
    print(
        "  by skill: "
        + ", ".join(
            f"{name} {sum(1 for j in of if j.right)}/{len(of)}"
            for name, of in sorted(by_skill.items())
        )
    )
    took = [j.seconds for j in judged if j.accepted]
    print(
        f"  a plan takes {statistics.median(took):.1f} s (median), "
        f"{percentile(took, 0.9):.1f} s (90th percentile)"
    )
    for j in judged:
        if j.refused is not None:
            print(f"  no plan: {j.case.skill} {j.case.values}: {j.refused}")
        elif j.problems:
            print(f"  wrong: {j.case.skill} {j.case.values} [{j.case.language}]: {j.problems}")

    # What must hold of a model worth binding to the role that writes plans: nearly every plan it
    # writes is one that the checks accept, and most of them do what the skill says. What the
    # checks accept is safe whatever the model wrote: this is about whether it is useful.
    assert sum(1 for j in judged if j.accepted) / len(judged) >= 0.9
    assert sum(1 for j in judged if j.right) / len(judged) >= 0.75


@dataclass(frozen=True, slots=True)
class Said:
    text: str
    speaker: str
    case: Case
    """What the skill is to do with it: which skill, and the values in what was said."""


MESSAGES = (
    Said("set the movie scene", KITCHEN, Case("movie-scene", {}, "en")),
    Said("prepara la scena per il film", DISPLAY, Case("movie-scene", {}, "it")),
    Said("how is the air quality in Rome", KITCHEN, Case("air-quality", {"place": "Rome"}, "en")),
    Said(
        "com'è la qualità dell'aria a Torino",
        DISPLAY,
        Case("air-quality", {"place": "Torino"}, "it"),
    ),
    Said("is the air clean in Milan", DISPLAY, Case("air-quality", {"place": "Milan"}, "en")),
    Said(
        "tell everyone that dinner is ready",
        KITCHEN,
        Case("announce", {"message": "dinner is ready"}, "en"),
    ),
    Said(
        "annuncia che la cena è pronta",
        DISPLAY,
        Case("announce", {"message": "la cena è pronta"}, "it"),
    ),
    Said(
        "say that it is time to go",
        KITCHEN,
        Case("announce", {"message": "it is time to go"}, "en"),
    ),
)


def wrong_announced(case: Case, answer: Answer, web: FakeWeb) -> list[str]:
    """What a person said is not what a model has to repeat letter for letter."""
    given = [c for c in answer["commands"] if c.get("name") == "speak"]
    wanted = str(case.values["message"]).casefold()
    if len(answer["commands"]) != 1 or len(given) != 1:
        return [f"it gave {answer['commands']}"]
    if given[0]["text"].strip().strip(".!").casefold() != wanted:
        return [f"it said {given[0]['text']!r}"]
    return ["it reached the web"] if web.seen else []


def pointed_at(text: str, web: FakeWeb) -> str:
    """The skill as it is, but reaching this test's web server: the hosts it names are its own."""
    named = ("geocoding-api.open-meteo.com", "air-quality-api.open-meteo.com")
    for host in named:
        text = text.replace(f"https://{host}", web.url(""))
    text = text.replace("homeassistant.local:8123", f"127.0.0.1:{web.port}")
    text = text.replace("on port 8123", f"on port {web.port}")
    for host in named:
        text = text.replace(host, "127.0.0.1")
    return text.replace("homeassistant.local", "127.0.0.1")


@pytest.mark.parametrize("model", WRITER_MODELS)
async def test_a_message_becomes_what_the_skill_says_through_both_models(
    model: str, tmp_path: Path
) -> None:
    opened = await open_gateway(model)
    took: list[float] = []
    problems: list[str] = []
    try:
        with FakeWeb() as web:
            serve(web)
            for name in sorted(SHIPPED):
                (tmp_path / name).mkdir()
                text = (EXAMPLES / name / "SKILL.md").read_text("utf-8")
                (tmp_path / name / "SKILL.md").write_text(pointed_at(text, web), encoding="utf-8")
            rig = ActionsRig(
                home_text=HOME_TEXT,
                chat=opened.chat,
                embedder=opened.embedder,
                patience_s=PATIENCE_S,
                instructions=True,
            )
            rig.use_local_runner(secrets=f'tenant:\n  HA_TOKEN: "{TOKEN}"\n')
            catalog = Catalog(
                rig.store, rig.index, folder=tmp_path, enable_folder=True, now=rig.clock
            )
            read = await catalog.reload(rig.principal(None))
            assert read.problems == ()
            assert len(read.enabled) == len(SHIPPED)
            # The first message loads the models: it is not what a message pays for.
            await rig.hear(KITCHEN, "set the movie scene")
            for said in MESSAGES:
                web.seen.clear()
                rig.dispatcher.given.clear()
                began = time.perf_counter()
                outcome = await rig.hear(said.speaker, said.text)
                took.append(time.perf_counter() - began)
                if outcome.status is not ActStatus.DONE or outcome.skill != said.case.skill:
                    wrong = [f"{outcome.status.value} {outcome.skill}: {outcome.reply!r}"]
                else:
                    answer: Answer = {
                        "message": outcome.reply,
                        "commands": [c.model_dump() for _, _, c in rig.dispatcher.given],
                    }
                    judge = (
                        wrong_announced
                        if said.case.skill == "announce"
                        else JUDGES[said.case.skill]
                    )
                    wrong = judge(said.case, answer, web)
                if wrong:
                    problems.append(f"{said.text!r} at {said.speaker}: {wrong}")
    finally:
        await close_gateway(opened)

    right = len(MESSAGES) - len(problems)
    print(f"\n  through both models ({FAST_MODEL} reads, {model} writes): {right}/{len(MESSAGES)}")
    print(
        f"  a message takes {statistics.median(took):.1f} s (median), "
        f"{max(took):.1f} s (slowest), the two models and the run together"
    )
    for problem in problems:
        print(f"  wrong: {problem}")
    assert right / len(MESSAGES) >= 0.75
