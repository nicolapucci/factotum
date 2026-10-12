"""A series of commands, said to the devices of the example home one after another, and what each of
them came to.

The messages are the corpus that a model that plans is measured against
(``factotum.actions.plan_cases``): more than eighty, in English and in Italian, said to the
kitchen speaker, the living room display and a phone, about the example skills. Some ask for
something to be done, and what becomes of each depends on where it is said; more are questions,
some dressed as commands. Each comes with what the code must make of it: done, asked about
first, refused, asked for what is missing, or left as a question.

What is made up is the reading of the message. The planner proposes what a competent one would for
the message that was *meant* (``HouseholdModel.proposal``), so that what is looked at is what the
rest of the assistant does with it: the way the words get to it, the checks of what was proposed,
the policy of the device they were said to, the yes that is asked for, the script or the plan that
runs (or, for a skill of the platform, the code of the actions service), the command that reaches a
device and the words that come back. How well a model reads a
message is the measure of ``services/actions/tests/live``; how well Whisper hears it is told here by
what the edge took each recording for. The same series is said with the stand-in speech, which is
fast and exact, and, for the part of it that is in Italian, with the real one.
"""

import asyncio
import difflib
import json
import re
import unicodedata
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

from factotum.actions.instructions.testing import Seen as WebRequest
from factotum.actions.instructions.testing import plan_dict, step_http
from factotum.actions.plan_cases import DISPLAY, KITCHEN, PHONE, PLAN_CASES, PlanCase
from factotum.contracts.actions import ActCode, ActOutcome, ActStatus
from factotum.contracts.audit import ActionResult, AuditRecord
from factotum.contracts.authorization import DecisionOutcome
from factotum.contracts.edge import TurnEnded, TurnStatus
from factotum.contracts.runner import Job
from factotum.e2e.devices import Device, connected
from factotum.e2e.house import House
from factotum.e2e.models import is_italian
from factotum.e2e.wiring import HA_TOKEN

PROFILES = {KITCHEN: "speaker", DISPLAY: "smart-display", PHONE: "phone"}
"""The profile of the terminal that stands for each device that the messages are said to."""

ENTITIES = {
    "Living room lamp": "light.living_room",
    "Bedroom lamp": "light.bedroom",
    "Garage door": "cover.garage",
}
"""What each external device of the example home is in Home Assistant (``devices.example.yaml``)."""

HOMEASSISTANT = "homeassistant.local"
GEOCODING_HOST = "geocoding-api.open-meteo.com"
AIR_HOST = "air-quality-api.open-meteo.com"
WEATHER_HOSTS = (GEOCODING_HOST, "api.open-meteo.com")

AUTHORIZATION = {"Authorization": "Bearer ${secret.HA_TOKEN}"}
LIGHTS = f"http://{HOMEASSISTANT}:8123/api/services/light"
GEOCODING = f"http://{GEOCODING_HOST}/v1/search?count=1&name=${{value.place}}"
AIR = (
    f"http://{AIR_HOST}/v1/air-quality?current=european_aqi"
    "&latitude=${step.place.json.results.0.latitude}"
    "&longitude=${step.place.json.results.0.longitude}"
)
"""The addresses of the plans that a model writes for the skills that are only instructions. They
say ``http`` where the notes of the skills say ``https``: the web server that every host is here
speaks plain http, and the checks accept either."""

AIR_SAID = "The air quality index in ${value.place} is ${step.air.json.current.european_aqi}."

PLACE_LATITUDE, PLACE_LONGITUDE, AIR_INDEX = 45.07, 7.69, 21
"""What the stand-in of Open-Meteo answers (``house.OPEN_METEO``)."""

YES = {"en": "Yes.", "it": "Sì."}
NO = {"en": "No.", "it": "No."}

EXTRA_CASES: tuple[PlanCase, ...] = (
    # The phone is the one device that may switch both lamps, which is where a lamp that is named
    # has to be told from the one that is not, and where a lamp that is not named is a question.
    PlanCase(
        "turn on the living room lamp",
        PHONE,
        "lamp",
        ActStatus.DONE,
        "Living room lamp",
        {"state": "on"},
    ),
    PlanCase(
        "accendi la luce del salotto",
        PHONE,
        "lamp",
        ActStatus.DONE,
        "Living room lamp",
        {"state": "on"},
    ),
    PlanCase("switch off the lamp", PHONE, "lamp", ActStatus.NEEDS_MORE, None, {"state": "off"}),
    # A lamp that is somebody's own does not exist for a speaker that is everybody's.
    PlanCase(
        "turn on my lamp", KITCHEN, "lamp", ActStatus.NEEDS_MORE, "Bedroom lamp", {"state": "on"}
    ),
)
"""What the corpus does not say is what becomes of a message at the device that has two lamps to
choose from, or at the one that has one that is not its own: the cases of the corpus name the lamp
that the device has, and a series that only tried those would not tell a lamp that is named from the
first one."""

SERIES = (*PLAN_CASES, *EXTRA_CASES)
"""What the series says: the corpus, and then these."""

NEEDS_MORE_FOR_A_DEVICE = frozenset({("switch off the lamp", PHONE), ("turn on my lamp", KITCHEN)})
"""The messages that ask which device is meant, and not what value is missing."""


def question_for(action: str, italian: bool) -> str:
    """The yes or no question that a competent planner proposes for the garage door."""
    if italian:
        return f"Vuoi davvero che {'chiuda' if action == 'close' else 'apra'} il garage?"
    return f"Do you really want me to {action} the garage door?"


def proposal_for(case: PlanCase) -> dict[str, Any]:
    """What a competent small model proposes for the message of this case, as the planner asks for
    it: the skill, the device it is for, the values, the language and, where a yes is to be asked
    for, the question."""
    italian = is_italian(case.message)
    plan: dict[str, Any] = {
        "reason": "it asks for something to be done" if case.skill else "it asks for nothing",
        "skill": case.skill,
        "device": case.device,
        "parameters": dict(case.params),
        "language": "it" if italian else "en",
        "question": None,
        "ask": None,
    }
    if case.status is ActStatus.NEEDS_CONFIRMATION:
        plan["question"] = question_for(str(case.params["action"]), italian)
    return plan


def writing_for(case: PlanCase) -> dict[str, Any] | None:
    """What a competent model writes as the plan of a skill that is only instructions, for what the
    case asks: what a plan is for is in the notes of each skill (``skills/examples``). Nothing for a
    skill that has a script, nothing for a skill of the platform (``announce`` is code, and no model
    writes what it does) and nothing for what is not going to be done."""
    if case.status is not ActStatus.DONE:
        return None
    match case.skill:
        case "movie-scene":
            return plan_dict(
                step_http(
                    "ceiling",
                    "POST",
                    f"{LIGHTS}/turn_off",
                    headers=AUTHORIZATION,
                    body={"entity_id": "light.living_room_ceiling"},
                ),
                step_http(
                    "backlight",
                    "POST",
                    f"{LIGHTS}/turn_on",
                    headers=AUTHORIZATION,
                    body={"entity_id": "light.tv_backlight", "brightness_pct": 20},
                ),
                say="The movie scene is set.",
            )
        case "air-quality":
            return plan_dict(
                step_http("place", "GET", GEOCODING, headers={}),
                step_http("air", "GET", AIR, headers={}),
                say=AIR_SAID,
            )
    return None


def weather_of(job: Job) -> dict[str, Any]:
    """What the weather skill prints, for the place it was asked about: it reaches Open-Meteo over
    https, which the stand-in of the web does not speak."""
    place = json.loads(job.stdin)["params"]["place"]
    return {"message": f"In {place} it is 21 degrees.", "data": {"temperature_2m": 21}}


def prepare(house: House) -> None:
    """What a series needs of the house that it does not have of its own: the skill that cannot run
    here is told what it prints."""
    house.runner.canned["weather"] = weather_of


@asynccontextmanager
async def devices_of(house: House) -> AsyncIterator[dict[str, Device]]:
    """The three devices that the messages are said to, connected and doing what the edge sends,
    by the name that ``PlanCase.speaker`` has for each."""
    async with AsyncExitStack() as stack:
        yield {
            name: await stack.enter_async_context(connected(house, profile))
            for name, profile in PROFILES.items()
        }


# ---- what happened ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """What came of one thing that was said, between the moment it began to be sent and the moment
    its answer had been given and played."""

    said: str
    heard: str | None
    """What the edge took the recording for: what it was told, before it began to answer."""

    ended: TurnEnded
    asked: int
    """How many times the actions service was asked about it."""

    outcome: ActOutcome | None
    """What the actions service made of it, when it was asked once."""

    jobs: tuple[Job, ...]
    web: tuple[WebRequest, ...]
    events: dict[str, tuple[str, ...]]
    """By device: what its hardware was told to do (a volume, a light)."""

    announced: dict[str, tuple[str, ...]]
    """By device: what its hardware was told to say (a ``speak`` that its edge did not make a voice
    of)."""

    sentences: dict[str, int]
    """By device: how many sentences its speaker was made to say, whatever the sound is: the answer
    to a person is one, and so is each clip that a skill had it play."""

    volumes: dict[str, int]
    """By device: the volume its hardware has when it is over."""

    spoken: dict[str, tuple[str, ...]] | None
    """By device: what its speaker was made to say, where that can be read back out of the sound
    (the clips of the stand-in of Piper carry their words)."""

    chat: int
    """How many answers the chat model wrote: a question is answered by it."""


@dataclass(frozen=True)
class Played:
    """A case, and what came of it: of what was said, and of the yes or the no that followed a
    question."""

    case: PlanCase
    first: Window
    answer: Window | None = None
    """What was said to a question (a yes at the display, which may, and a no at the phone)."""

    answered_with: str | None = None


@dataclass(frozen=True)
class _DeviceMark:
    events: int
    announced: int
    sentences: int
    heard: int


@dataclass(frozen=True)
class _Mark:
    acts: int
    jobs: int
    web: int
    prompts: int
    devices: dict[str, _DeviceMark] = field(default_factory=dict)


Say = Callable[[Device, str], Awaitable[TurnEnded]]
"""How words are said to a device: a recording of them, the way the device carries it
(``Device.says_aloud``), or the voice of a person (``Device.sends(Person.saying(...))``)."""


def follows_with_a_yes(case: PlanCase) -> bool:
    """What is said to the question that a case is. The display has been allowed to start what
    cannot be undone, and a yes opens the door; a phone is allowed to as well, and is answered with
    a no, which leaves it shut: the series tries both."""
    return case.speaker == DISPLAY


class Series:
    """Cases said one after another to the same three devices, on the connections they hold."""

    def __init__(
        self,
        house: House,
        devices: dict[str, Device],
        say: Say,
        *,
        readable: bool = True,
    ) -> None:
        self._house = house
        self._devices = devices
        self._say = say
        self._readable = readable
        prepare(house)

    async def play(self, case: PlanCase) -> Played:
        """Says the message of the case to its device, with the planner told what it is to propose,
        and the yes or no that a question is asked for."""
        model = self._house.model
        model.proposal, model.writing = proposal_for(case), writing_for(case)
        try:
            first = await self._window(case.speaker, case.message)
            if case.status is not ActStatus.NEEDS_CONFIRMATION:
                return Played(case, first)
            yes = follows_with_a_yes(case)
            language = "it" if is_italian(case.message) else "en"
            said = (YES if yes else NO)[language]
            return Played(case, first, await self._window(case.speaker, said), said)
        finally:
            model.proposal = model.writing = None

    async def run(self, cases: tuple[PlanCase, ...]) -> list[Played]:
        return [await self.play(case) for case in cases]

    def _mark(self) -> _Mark:
        house = self._house
        return _Mark(
            acts=len(house.acts),
            jobs=len(house.runner.jobs),
            web=len(house.home_assistant.seen),
            prompts=len(house.model.prompts),
            devices={
                name: _DeviceMark(
                    events=len(device.shown.of("event")),
                    announced=len(device.hardware.state.spoken),
                    sentences=len(device.sentences()),
                    heard=len(device.understood()),
                )
                for name, device in self._devices.items()
            },
        )

    async def _window(self, speaker: str, words: str) -> Window:
        before = self._mark()
        ended = await self._say(self._devices[speaker], words)
        for device in self._devices.values():
            # What a skill had another device say is played there while the answer is given here.
            await asyncio.wait_for(device.client.wait_until_played(), device.timeout_s)
        house = self._house
        acts = house.acts[before.acts :]
        heard = self._devices[speaker].understood()[before.devices[speaker].heard :]
        return Window(
            said=words,
            heard=heard[0] if len(heard) == 1 else None,
            ended=ended,
            asked=len(acts),
            outcome=acts[0] if len(acts) == 1 else None,
            jobs=tuple(house.runner.jobs[before.jobs :]),
            web=tuple(house.home_assistant.seen[before.web :]),
            events={
                name: tuple(device.shown.of("event")[before.devices[name].events :])
                for name, device in self._devices.items()
            },
            announced={
                name: tuple(device.hardware.state.spoken[before.devices[name].announced :])
                for name, device in self._devices.items()
            },
            volumes={name: device.hardware.state.volume for name, device in self._devices.items()},
            sentences={
                name: len(device.sentences()) - before.devices[name].sentences
                for name, device in self._devices.items()
            },
            spoken=(
                {
                    name: tuple(device.spoken()[before.devices[name].sentences :])
                    for name, device in self._devices.items()
                }
                if self._readable
                else None
            ),
            chat=len(house.model.prompts) - before.prompts,
        )


# ---- what each came to --------------------------------------------------------------------------


def judge(played: Played) -> list[str]:
    """What is wrong with what came of a case, in a few words each; nothing when it is right."""
    case, first = played.case, played.first
    problems: list[str] = []
    if first.ended.status is not TurnStatus.FINISHED:
        problems.append(f"the turn ended {first.ended.status.value}")
    outcome = first.outcome
    if outcome is None:
        return [*problems, f"the actions service was asked about it {first.asked} times"]
    if outcome.status is not case.status:
        problems.append(f"{outcome.status.value} instead of {case.status.value}")
    if outcome.skill != case.skill:
        problems.append(f"skill {outcome.skill} instead of {case.skill}")
    if problems:
        return problems  # what follows is about what was meant, and that is not what happened
    if (first.chat > 0) is not (case.status is ActStatus.NO_SKILL):
        problems.append(f"the chat model wrote {first.chat} answers")
    match case.status:
        case ActStatus.DONE:
            problems += _done(case, first)
        case ActStatus.NEEDS_CONFIRMATION:
            problems += _asked(case, played)
        case ActStatus.NEEDS_MORE:
            problems += _nothing_happened(first)
            code = (
                ActCode.AMBIGUOUS_DEVICE
                if (case.message, case.speaker) in NEEDS_MORE_FOR_A_DEVICE
                else ActCode.MISSING_VALUES
            )
            if outcome.code is not code:
                problems.append(f"code {outcome.code} instead of {code.value}")
        case ActStatus.REFUSED:
            problems += _nothing_happened(first)
            wanted = ActCode.DENIED if case.skill == "garage-door" else ActCode.APPROVAL_UNAVAILABLE
            if outcome.code is not wanted:
                problems.append(f"code {outcome.code} instead of {wanted.value}")
        case _:
            problems += _nothing_happened(first)
    return problems


def _nothing_happened(window: Window) -> list[str]:
    """Nothing ran, nothing was reached and no device was told to do anything."""
    problems: list[str] = []
    if window.jobs:
        problems.append(f"it ran {[job.label for job in window.jobs]}")
    if window.web:
        problems.append(f"it reached {[request.target for request in window.web]}")
    for name, events in window.events.items():
        if events:
            problems.append(f"{name} was told {list(events)}")
    for name, said in window.announced.items():
        if said:
            problems.append(f"{name} was told to say {list(said)}")
    return problems


def _job(window: Window, skill: str, hosts: tuple[str, ...], env: dict[str, str]) -> list[str]:
    """The skill was run once, with the hosts and the secrets that it declared and no others."""
    labels = [job.label for job in window.jobs]
    if labels != [skill]:
        return [f"it ran {labels} instead of {[skill]}"]
    [job] = window.jobs
    problems: list[str] = []
    if job.hosts != hosts:
        problems.append(f"the job could reach {job.hosts} instead of {hosts}")
    if job.env != env:
        problems.append(f"the job was given {sorted(job.env)} instead of {sorted(env)}")
    return problems


def _calls(window: Window) -> list[tuple[str, str, Any]]:
    return [
        (request.method, urlsplit(request.target).path, request.json() if request.body else None)
        for request in window.web
    ]


def _done(case: PlanCase, window: Window) -> list[str]:
    problems: list[str] = []
    place = case.params.get("place")
    spoken = " ".join(window.spoken[case.speaker]) if window.spoken is not None else None
    match case.skill:
        case "lamp":
            wanted: dict[str, Any] = {"entity_id": ENTITIES[str(case.device)]}
            if case.params["state"] == "on" and "brightness" in case.params:
                wanted["brightness_pct"] = case.params["brightness"]
            expected = [("POST", f"/api/services/light/turn_{case.params['state']}", wanted)]
            if _calls(window) != expected:
                problems.append(f"Home Assistant was asked {_calls(window)} instead of {expected}")
            if any(
                request.headers.get("authorization") != f"Bearer {HA_TOKEN}"
                for request in window.web
            ):
                problems.append("Home Assistant was not given the token of the home")
            problems += _job(window, "lamp", (HOMEASSISTANT,), {"HA_TOKEN": HA_TOKEN})
        case "volume":
            expected_events = (f"volume {case.params['level']}",)
            if window.events[case.speaker] != expected_events:
                problems.append(f"{case.speaker} was told {window.events[case.speaker]}")
            if window.volumes[case.speaker] != case.params["level"]:
                problems.append(f"{case.speaker} has its volume at {window.volumes[case.speaker]}")
            problems += _job(window, "volume", (), {})
            problems += _nothing_else_was_reached(window)
        case "announce":
            problems += _announced(case, window)
        case "movie-scene":
            lights = "/api/services/light/turn_"
            expected = [
                ("POST", f"{lights}off", {"entity_id": "light.living_room_ceiling"}),
                (
                    "POST",
                    f"{lights}on",
                    {"entity_id": "light.tv_backlight", "brightness_pct": 20},
                ),
            ]
            if _calls(window) != expected:
                problems.append(f"Home Assistant was asked {_calls(window)} instead of {expected}")
            problems += _job(window, "movie-scene", (HOMEASSISTANT,), {"HA_TOKEN": HA_TOKEN})
        case "weather":
            problems += _job(window, "weather", WEATHER_HOSTS, {})
            if window.jobs and json.loads(window.jobs[0].stdin)["params"] != {"place": place}:
                problems.append(f"the place was {json.loads(window.jobs[0].stdin)['params']}")
            if spoken is not None and str(place) not in spoken:
                problems.append(f"{place} is not in what was said: {spoken!r}")
        case "air-quality":
            problems += _job(window, "air-quality", (GEOCODING_HOST, AIR_HOST), {})
            problems += _air_reads(window, str(place))
            if spoken is not None and (str(place) not in spoken or str(AIR_INDEX) not in spoken):
                problems.append(f"the place and the index are not in what was said: {spoken!r}")
        case _:
            problems.append(f"the series does not know what {case.skill} is to do")
    return problems


def _announced(case: PlanCase, window: Window) -> list[str]:
    """An announcement is the code of the actions service and nothing that a sandbox runs: no job,
    nothing reached on the network, no word handed to a device to say (what a device is given is a
    clip, which its speaker plays). The message is said once on the device that it was for, and each
    speaker says the answer that the person was given besides, where they were spoken to."""
    message = str(case.params["message"])
    target = case.device or case.speaker
    problems = _nothing_else_was_reached(window)
    if window.jobs:
        problems.append(f"it ran {[job.label for job in window.jobs]}")
    told = {name: said for name, said in window.announced.items() if said}
    if told:
        problems.append(f"{told} was told to speak words, and an announcement is a clip")
    sentences = dict.fromkeys(window.sentences, 0)
    sentences[case.speaker] += 1  # the answer to the person: "Done."
    sentences[target] += 1  # the message
    if window.sentences != sentences:
        problems.append(f"{window.sentences} sentences were said instead of {sentences}")
    if window.spoken is not None and window.outcome is not None:
        wanted: dict[str, list[str]] = {name: [] for name in window.spoken}
        wanted[case.speaker].append(window.outcome.reply or "")
        wanted[target].append(message)
        said = {name: sorted(words) for name, words in window.spoken.items()}
        if said != {name: sorted(words) for name, words in wanted.items()}:
            problems.append(f"{said} was said instead of {wanted}")
    return problems


def _nothing_else_was_reached(window: Window) -> list[str]:
    return [f"it reached {[request.target for request in window.web]}"] if window.web else []


def _air_reads(window: Window, place: str) -> list[str]:
    """The plan looked the place up and then read the air at it, and only that."""
    seen = [urlsplit(request.target) for request in window.web]
    problems: list[str] = []
    if [(url.hostname, url.path) for url in seen] != [
        (GEOCODING_HOST, "/v1/search"),
        (AIR_HOST, "/v1/air-quality"),
    ]:
        return [f"the air was read through {[url.geturl() for url in seen]}"]
    if parse_qs(seen[0].query).get("name") != [place]:
        problems.append(f"the place that was looked up was {parse_qs(seen[0].query).get('name')}")
    air = parse_qs(seen[1].query)
    if air.get("latitude") != [str(PLACE_LATITUDE)] or air.get("longitude") != [
        str(PLACE_LONGITUDE)
    ]:
        problems.append(f"the air was read at {air.get('latitude')}, {air.get('longitude')}")
    return problems


def _asked(case: PlanCase, played: Played) -> list[str]:
    """It asks first, and nothing moves until the answer: a yes is done, a no leaves things as
    they are."""
    first = played.first
    problems = _nothing_happened(first)
    if first.outcome is not None and first.outcome.code is not None:
        problems.append(f"it was refused: {first.outcome.code.value}")
    answer = played.answer
    if answer is None or answer.outcome is None:
        return [*problems, "what was asked was not answered"]
    if follows_with_a_yes(case):
        if answer.outcome.status is not ActStatus.DONE:
            return [*problems, f"a yes was {answer.outcome.status.value}"]
        door = f"/api/services/cover/{case.params['action']}_cover"
        expected = [("POST", door, {"entity_id": ENTITIES["Garage door"]})]
        if _calls(answer) != expected:
            problems.append(f"Home Assistant was asked {_calls(answer)} instead of {expected}")
        return problems + _job(answer, "garage-door", (HOMEASSISTANT,), {"HA_TOKEN": HA_TOKEN})
    if answer.outcome.status is not ActStatus.CANCELLED:
        problems.append(f"a no was {answer.outcome.status.value}")
    return problems + _nothing_happened(answer)


# ---- what the whole series left behind -----------------------------------------------------------


def trail_of(played: list[Played]) -> list[tuple[str, DecisionOutcome, ActionResult, bool | None]]:
    """What the audit trail must say after the series, the oldest first: a step for what was done,
    refused and asked about, and one more for the answer to a question. What asks for more, and
    what is only a question, leaves none."""
    steps: list[tuple[str, DecisionOutcome, ActionResult, bool | None]] = []
    for each in played:
        case = each.case
        skill = case.skill or ""
        match case.status:
            case ActStatus.DONE:
                steps.append((skill, DecisionOutcome.ALLOW, ActionResult.EXECUTED, None))
            case ActStatus.REFUSED:
                decision = (
                    DecisionOutcome.DENY
                    if skill == "garage-door"
                    else DecisionOutcome.REQUIRE_APPROVAL
                )
                steps.append((skill, decision, ActionResult.NOT_EXECUTED, None))
            case ActStatus.NEEDS_CONFIRMATION:
                asked = DecisionOutcome.REQUIRE_CONFIRMATION
                steps.append((skill, asked, ActionResult.NOT_EXECUTED, None))
                yes = follows_with_a_yes(case)
                result = ActionResult.EXECUTED if yes else ActionResult.NOT_EXECUTED
                steps.append((skill, asked, result, yes))
    return steps


def as_steps(
    records: tuple[AuditRecord, ...],
) -> list[tuple[str, DecisionOutcome, ActionResult, bool | None]]:
    """The audit trail as ``trail_of`` says it, the oldest first (the trail lists the latest)."""
    return [(r.skill_name, r.decision, r.result, r.confirmed) for r in reversed(records)]


def problems_of(played: list[Played]) -> list[str]:
    """What is wrong with each case that came to what it was not meant to, as a line each."""
    return [
        f"{each.case.message!r} at {each.case.speaker}: {'; '.join(found)}"
        for each in played
        if (found := judge(each))
    ]


# ---- what was heard -----------------------------------------------------------------------------


def plain(text: str) -> str:
    """What was heard, in lower case, without accents or punctuation: to compare with what was
    said, as a person does who does not mind that a Whisper wrote ``Si`` for ``Sì``."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    letters = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", letters).strip()


def likeness(heard: str, said: str) -> float:
    """How much of what was said is what was heard, from nothing to everything."""
    return difflib.SequenceMatcher(None, plain(heard), plain(said)).ratio()


def nearest(heard: str, messages: Iterable[str]) -> str:
    """Which of the messages of a series what was heard is nearest to: the one a reader who knows
    them all would take it for."""
    return max(messages, key=lambda message: likeness(heard, message))


def table_of(played: list[Played]) -> list[str]:
    """What was said and what was heard of it, with how alike, a line each: what a person who runs
    the series with a real model reads."""
    return [
        f"{likeness(each.first.heard or '', each.case.message):4.2f}  "
        f"{each.case.message!r} heard as {each.first.heard!r}"
        for each in played
    ]
