"""Asking a model for the plan of a skill that has no script.

The model is given the instructions of the skill, what the person asked for, what the skill may
reach and the language of the message, and answers with a plan: data that the platform checks
(``checks``) and then runs in the sandbox (``executor``). It is asked in the ``chat.main`` role,
because writing requests that fit an API is what a better model is for and this is the one place
that needs it, and it is asked the way the planner is (``planner``): the examples are turns of the
conversation the model is in, and the answer says *why* before it says what.

What it writes is a proposal. If the checks refuse it, the model is told which step and why and
asked once more; what it wrote the second time is checked the same way. It never sees a secret (only
the name of one) and never anything that came from a document or a page: the instructions are the
skill's, reviewed when it was enabled, and the values are the person's own words.
"""

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from factotum.actions.instructions.checks import PlanRejected
from factotum.contracts.chat import ChatMessage, ChatRequest, MessageRole
from factotum.contracts.effects import Effect
from factotum.contracts.errors import PlanError, ProviderTimeoutError
from factotum.contracts.identity import Owner
from factotum.contracts.instructions import InstructionPlan
from factotum.contracts.models import ModelRole
from factotum.contracts.skills import ParamValue
from factotum.model_client import Chat

MAX_FEEDBACK_CHARS = 400
MAX_FIELD_NAME_CHARS = 40
"""The name of a field that a model wrote is what it wrote, so only the start of it is quoted."""


def _inline(schema: Any, definitions: Mapping[str, Any]) -> Any:
    """A JSON Schema without references and titles: what a model is asked to answer with is one
    piece, which is what every provider handles."""
    if isinstance(schema, dict):
        reference = schema.get("$ref")
        if isinstance(reference, str):
            return _inline(definitions[reference.rsplit("/", 1)[-1]], definitions)
        return {
            name: _inline(item, definitions)
            for name, item in schema.items()
            if name not in {"$defs", "title"}
        }
    if isinstance(schema, list):
        return [_inline(item, definitions) for item in schema]
    return schema


def _response_schema() -> dict[str, Any]:
    schema = InstructionPlan.model_json_schema()
    inlined: dict[str, Any] = _inline(schema, schema.get("$defs", {}))
    return inlined


RESPONSE_SCHEMA: dict[str, Any] = _response_schema()
"""What a model is asked to answer with: the shape of ``InstructionPlan``."""


SYSTEM_PROMPT = """\
You write the plan that carries out one skill of a voice assistant. You are given the skill's \
instructions, the values the person asked for, what the skill may reach, and the language of the \
message. You never run anything: you write a plan, as JSON, and the platform checks it and runs \
it. Answer with JSON only.

"reason" is a few words on what the plan does.

"steps" is what is done, in this order, at most 8. Each step has "id" (a short name in lower \
case), "kind" ("http" or "command") and only the fields of its kind:
- "http": "method" (GET, POST, PUT, PATCH or DELETE), "url" (a full http or https address), \
"headers" (an object, {} when there are none), "body" (a JSON value, which is sent as JSON, or a \
string, which is sent as text, or null).
- "command": "name" and "args" (an object). The commands are: set_volume(level 0 to 100), \
speak(text), play_url(url, kind, title), pause, resume, stop, show_card, clear_display, \
set_led(pattern, color).
The fields that are not of the step's kind are null, or {} for "headers" and "args".

"say" is what to tell the person when everything is done: a short sentence in the language of the \
message, or null.

Never write out a secret or a value of the device or of the message: write a placeholder, and the \
platform puts the value in when the plan runs.
${secret.NAME} is a secret the skill may use, in a header, a body or an address.
${device.NAME} is a value of the device the skill is bound to.
${value.NAME} is a value the person asked for. A body that is exactly one placeholder gets the \
value with its own type: a number stays a number.
${step.ID.json.PATH}, ${step.ID.status} and ${step.ID.text} are what an earlier request got. PATH \
is field names and list positions separated by dots, like results.0.latitude. Put |round after a \
number to round it, as in ${step.ID.json.PATH|round}.
Only reach the hosts you are given, and only use the placeholders you are shown. A skill that only \
reads uses GET only. Never put a secret in "say" or in a command.
If the instructions cannot be followed with what you are given, answer with no steps and say why \
in "reason".\
"""


@dataclass(frozen=True, slots=True)
class PlanInputs:
    """What the model is given for one run of a skill."""

    name: str
    description: str
    instructions: str
    effect: Effect
    values: Mapping[str, ParamValue]
    """What the person asked for, by the names the skill declares."""

    device_values: tuple[str, ...]
    """The *names* of what is fixed for the device: what it is is put in by the platform."""

    hosts: tuple[str, ...]
    secrets: tuple[str, ...]
    """The *names* of the secrets: never what they are."""

    language: str = "en"


def render(inputs: PlanInputs) -> str:
    """One turn of what the model is asked."""
    lines = [
        f"Skill: {inputs.name} ({inputs.effect.value.replace('_', ' ')})",
        f"What it does: {inputs.description}",
        "Instructions:",
        inputs.instructions.strip(),
        f"Values asked for (JSON): {json.dumps(dict(inputs.values), ensure_ascii=False)}",
        f"Values of the device: {', '.join(inputs.device_values) or 'none'}",
        f"Hosts you may reach: {', '.join(inputs.hosts) or 'none'}",
        f"Secrets you may use: {', '.join(inputs.secrets) or 'none'}",
        f"Language of the message: {inputs.language}",
    ]
    if inputs.effect is Effect.READ_ONLY:
        lines.append("The skill only reads: use GET only.")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class Example:
    inputs: PlanInputs
    plan: dict[str, Any]

    def user(self) -> str:
        return render(self.inputs)

    def answer(self) -> str:
        return json.dumps(self.plan, ensure_ascii=False, separators=(",", ":"))


def _step(**fields: Any) -> dict[str, Any]:
    return {
        "method": None,
        "url": None,
        "headers": {},
        "body": None,
        "name": None,
        "args": {},
        **fields,
    }


EXAMPLES: tuple[Example, ...] = (
    Example(
        PlanInputs(
            name="lamp",
            description="Turn a lamp on or off, or set how bright it is.",
            instructions=(
                "Use the REST API of Home Assistant. To switch the lamp on, POST "
                "http://HOST:8123/api/services/light/turn_on with brightness_pct when a "
                "brightness is asked for; to switch it off, POST "
                'http://HOST:8123/api/services/light/turn_off. Send {"entity_id": ENTITY} as '
                "JSON, with the header Authorization: Bearer TOKEN."
            ),
            effect=Effect.REVERSIBLE,
            values={"state": "on", "brightness": 40},
            device_values=("entity_id", "host"),
            hosts=("ha.example",),
            secrets=("HA_TOKEN",),
        ),
        {
            "reason": "switches the lamp on at 40 percent",
            "steps": [
                _step(
                    id="lamp",
                    kind="http",
                    method="POST",
                    url="http://${device.host}:8123/api/services/light/turn_on",
                    headers={"Authorization": "Bearer ${secret.HA_TOKEN}"},
                    body={
                        "entity_id": "${device.entity_id}",
                        "brightness_pct": "${value.brightness}",
                    },
                )
            ],
            "say": "The lamp is on.",
        },
    ),
    Example(
        PlanInputs(
            name="weather",
            description="Say what the weather is like now in a city.",
            instructions=(
                "Look the place up with https://geocoding-api.open-meteo.com/v1/search?count=1"
                "&name=PLACE (the answer has results, a list, and each has name, latitude and "
                "longitude). Then read https://api.open-meteo.com/v1/forecast?current="
                "temperature_2m&latitude=LAT&longitude=LON (the answer has current.temperature_2m, "
                "in degrees). Say the temperature."
            ),
            effect=Effect.READ_ONLY,
            values={"place": "Torino"},
            device_values=(),
            hosts=("geocoding-api.open-meteo.com", "api.open-meteo.com"),
            secrets=(),
            language="it",
        ),
        {
            "reason": "finds the place, then reads its temperature",
            "steps": [
                _step(
                    id="place",
                    kind="http",
                    method="GET",
                    url=(
                        "https://geocoding-api.open-meteo.com/v1/search?count=1&name=${value.place}"
                    ),
                ),
                _step(
                    id="now",
                    kind="http",
                    method="GET",
                    url=(
                        "https://api.open-meteo.com/v1/forecast?current=temperature_2m"
                        "&latitude=${step.place.json.results.0.latitude}"
                        "&longitude=${step.place.json.results.0.longitude}"
                    ),
                ),
            ],
            "say": "A ${value.place} ci sono ${step.now.json.current.temperature_2m|round} gradi.",
        },
    ),
    Example(
        PlanInputs(
            name="announce",
            description="Say something out loud on a device.",
            instructions="Give the device a speak command with the message as its text.",
            effect=Effect.REVERSIBLE,
            values={"message": "Dinner is ready"},
            device_values=(),
            hosts=(),
            secrets=(),
        ),
        {
            "reason": "has the device say the message",
            "steps": [
                _step(id="say", kind="command", name="speak", args={"text": "${value.message}"})
            ],
            "say": None,
        },
    ),
    Example(
        PlanInputs(
            name="turn-on-tv",
            description="Turn on the television.",
            instructions=(
                "Open the remote control application on the phone and press the power key."
            ),
            effect=Effect.REVERSIBLE,
            values={},
            device_values=(),
            hosts=(),
            secrets=(),
        ),
        {
            "reason": "the instructions need an application on a phone, which a plan cannot use",
            "steps": [],
            "say": None,
        },
    ),
)


def _shown() -> tuple[ChatMessage, ...]:
    turns: list[ChatMessage] = []
    for example in EXAMPLES:
        turns.append(ChatMessage(role=MessageRole.USER, content=example.user()))
        turns.append(ChatMessage(role=MessageRole.ASSISTANT, content=example.answer()))
    return tuple(turns)


_SHOWN = _shown()


def _problems(error: ValidationError) -> str:
    """What is wrong with a plan that is not the shape of one, field by field, and never what it
    held."""
    text = "; ".join(
        f"{'.'.join(str(part)[:MAX_FIELD_NAME_CHARS] for part in problem['loc']) or 'the plan'}: "
        f"{problem['msg']}"
        for problem in error.errors(include_input=False, include_context=False, include_url=False)
    )
    return text if len(text) <= MAX_FEEDBACK_CHARS else text[: MAX_FEEDBACK_CHARS - 1] + "…"


class PlanWriter:
    def __init__(self, chat: Chat, *, timeout_s: float = 30.0, attempts: int = 2) -> None:
        self._chat = chat
        self._timeout_s = timeout_s
        self._attempts = attempts

    async def write(
        self,
        owner: Owner,
        inputs: PlanInputs,
        check: Callable[[InstructionPlan], None],
    ) -> InstructionPlan:
        """A plan that ``check`` accepts. ``check`` raises ``PlanRejected`` for one it does not,
        and the model is told why and asked again, until it is out of attempts. Raises
        ``PlanError`` when no plan was accepted, and a ``FactotumError`` when the gateway could not
        be asked or the model did not answer in time."""
        messages = [
            ChatMessage(role=MessageRole.SYSTEM, content=SYSTEM_PROMPT),
            *_SHOWN,
            ChatMessage(role=MessageRole.USER, content=render(inputs)),
        ]
        why = "the model did not write a plan"
        for attempt in range(self._attempts):
            request = ChatRequest(
                role=ModelRole.CHAT_MAIN,
                messages=tuple(messages),
                response_schema=RESPONSE_SCHEMA,
                temperature=0,
                max_output_tokens=1000,
            )
            try:
                async with asyncio.timeout(self._timeout_s):
                    result = await self._chat.chat(owner, request)
            except TimeoutError:
                raise ProviderTimeoutError("the model did not write the plan in time") from None
            try:
                plan = InstructionPlan.model_validate(result.parsed)
                check(plan)
            except ValidationError as error:
                why = _problems(error)
            except PlanRejected as rejected:
                why = str(rejected)
            else:
                return plan
            if attempt + 1 < self._attempts:
                messages.extend(
                    [
                        ChatMessage(
                            role=MessageRole.ASSISTANT,
                            content=json.dumps(result.parsed, ensure_ascii=False)
                            if isinstance(result.parsed, dict)
                            else (result.text or "{}"),
                        ),
                        ChatMessage(
                            role=MessageRole.USER,
                            content=f"That plan was not accepted: {why} Write the plan again, "
                            "fixing that.",
                        ),
                    ]
                )
        raise PlanError(f"the plan was not accepted: {why}")
