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
from typing import Any, get_args

from pydantic import ValidationError

from factotum.actions.instructions.checks import PlanRejected
from factotum.contracts.chat import ChatMessage, ChatRequest, MessageRole
from factotum.contracts.effects import Effect
from factotum.contracts.errors import PlanError, ProviderTimeoutError
from factotum.contracts.identity import Owner
from factotum.contracts.instructions import STEP_ID_PATTERN, HttpMethod, InstructionPlan
from factotum.contracts.models import ModelRole
from factotum.contracts.skills import ParamValue
from factotum.model_client import Chat

MAX_FEEDBACK_CHARS = 400
RETRY_TEMPERATURE = 0.3
"""A model that is asked again with nothing changed but a sentence sometimes writes the same plan
(at temperature 0 it is the same answer to nearly the same question), so it is asked again with a
little room."""
MAX_FIELD_NAME_CHARS = 40
"""The name of a field that a model wrote is what it wrote, so only the start of it is quoted."""


_STRING: dict[str, Any] = {"type": "string"}
NO_BODY = ("GET", "DELETE")
"""The methods of a request that has no body, and is written without the field."""

_BODY: dict[str, Any] = {
    "anyOf": [{"type": "object"}, {"type": "string"}, {"type": "array"}, {"type": "null"}]
}
"""What a body may be: JSON (an object or a list) or a text. It has to say so, because a schema that
says nothing (``{}``, a value of any kind) is an *object* to the grammar that Ollama makes of it: a
model that was asked for a text there wrote ``{"hello world": ""}``, and every plain-text body that
a skill wanted (a notification, a line in a log) came out as JSON."""


def _step_of(kind: str, **fields: dict[str, Any]) -> dict[str, Any]:
    properties = {
        "id": {"type": "string", "pattern": STEP_ID_PATTERN},
        "kind": {"type": "string", "enum": [kind]},
        **fields,
    }
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _request_of(methods: tuple[str, ...], **fields: dict[str, Any]) -> dict[str, Any]:
    return _step_of(
        "http",
        method={"type": "string", "enum": list(methods)},
        url=_STRING,
        headers={"type": "object", "additionalProperties": _STRING},
        **fields,
    )


RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reason": _STRING,
        "steps": {
            "type": "array",
            "items": {
                "anyOf": [
                    _request_of(NO_BODY),
                    _request_of(
                        tuple(method for method in get_args(HttpMethod) if method not in NO_BODY),
                        body=_BODY,
                    ),
                    _step_of("command", name=_STRING, args={"type": "object"}),
                ]
            },
        },
        "say": {"anyOf": [_STRING, {"type": "null"}]},
    },
    "required": ["reason", "steps", "say"],
    "additionalProperties": False,
}
"""What a model is asked to answer with: a plan, as ``InstructionPlan`` reads it, but *written* more
strictly than it is read. A step has the fields of its kind and no others (a model that is asked to
fill in the ones that do not apply with ``null`` sometimes puts them where they do not belong, and
one that may write a body for a ``GET`` does), every field is one that it writes and in this order
(a model that may leave a field out sometimes leaves out the address of a request), a step is
called by a name that the placeholders can refer to, and there is no limit of size in it: those are
checked when the answer is read, because the grammar that Ollama makes of a schema fails for a
``maxLength`` of exactly 2000 (an address is that long; 1999 and 2001 are taken), and a plan that is
too long is a reason to write it again and not a way to make the model unable to write it."""


SYSTEM_PROMPT = """\
You write the plan that carries out one skill of a voice assistant. You are given the skill's \
instructions, the values the person asked for, what the skill may reach, and the language of the \
message. You never run anything: you write a plan, as JSON, and the platform checks it and runs \
it. Answer with JSON only.

"reason" is a few words on what the plan does.

"steps" is what is done, in this order, at most 8. A step is one of two kinds, and has the fields \
of its kind and no others; its "id" is a short name in lower case.
- "kind": "http" is a request: "method" (GET, POST, PUT, PATCH or DELETE), "url" (a full http or \
https address), "headers" (an object, {} when there are none) and, unless the method is GET or \
DELETE, "body" (a JSON value, which is sent as JSON, or a string, which is sent as text, or null).
- "kind": "command" is something a device is to do: "name" and "args" (an object). The commands \
are: set_volume(level 0 to 100), adjust_volume(direction up or down, and amount 1 to 100 only if \
the person said it), set_mute(muted true or false), speak(text), play_url(url, kind, title), \
pause, resume, stop, show_card, clear_display, set_led(pattern, color).

"say" is what to tell the person when everything is done: a short sentence in the language of the \
message, or null.

Never write out a secret or a value of the device or of the message: write a placeholder, and the \
platform puts the value in when the plan runs.
${secret.NAME} is a secret the skill may use, in a header, a body or an address.
${device.NAME} is a value of the device the skill is bound to.
${value.NAME} is a value the person asked for. A body that is exactly one placeholder gets the \
value with its own type: a number stays a number.
${step.ID.json.PATH}, ${step.ID.status} and ${step.ID.text} are what an earlier request got. PATH \
is field names and list positions separated by dots, like results.0.latitude. To round a number \
that a request got when you say it, put |round after it, as in ${step.ID.json.PATH|round}. What \
the person asked for is never rounded.
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


def _http(
    step: str, method: str, url: str, *, headers: dict[str, str] | None = None, body: Any = None
) -> dict[str, Any]:
    """A request as a model writes it: every field, in the order of the schema it answers with (an
    example that is written in another order teaches the model another order)."""
    request = {"id": step, "kind": "http", "method": method, "url": url, "headers": headers or {}}
    return request if method in NO_BODY else {**request, "body": body}


def _command(step: str, name: str, **args: Any) -> dict[str, Any]:
    return {"id": step, "kind": "command", "name": name, "args": args}


# One example for each shape of plan that a skill comes to (a request that changes something, one
# that only reads, a chain, a command, and nothing): a small model writes the shape of the examples
# it is shown. With only a chain of two requests that read (the weather), a skill that is one
# request that reads was written with steps that nobody asked for, and none of 18 plans was right;
# with an example of one request that reads, every one of them was (docs/skills.md).
EXAMPLES: tuple[Example, ...] = (
    Example(
        PlanInputs(
            name="lamp",
            description="Turn a lamp on or off, or set how bright it is.",
            instructions=(
                "Use the REST API of Home Assistant. To switch the lamp on, POST "
                "http://HOST:8123/api/services/light/turn_on with brightness_pct when a "
                "brightness is asked for, and transition 2; to switch it off, POST "
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
                _http(
                    "lamp",
                    "POST",
                    "http://${device.host}:8123/api/services/light/turn_on",
                    headers={"Authorization": "Bearer ${secret.HA_TOKEN}"},
                    body={
                        "entity_id": "${device.entity_id}",
                        "brightness_pct": "${value.brightness}",
                        "transition": 2,
                    },
                )
            ],
            "say": "The lamp is on.",
        },
    ),
    Example(
        PlanInputs(
            name="coin-price",
            description="Say what a coin costs now, in euros.",
            instructions=(
                "GET `https://api.coinbase.com/v2/prices/COIN-EUR/spot`. The answer has "
                "`data.amount`, the price in euros.\n\nSay the price, in the language of the "
                "message."
            ),
            effect=Effect.READ_ONLY,
            values={"coin": "BTC"},
            device_values=(),
            hosts=("api.coinbase.com",),
            secrets=(),
        ),
        {
            "reason": "reads the price of the coin",
            "steps": [
                _http("price", "GET", "https://api.coinbase.com/v2/prices/${value.coin}-EUR/spot")
            ],
            "say": "One ${value.coin} costs ${step.price.json.data.amount} euros.",
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
                _http(
                    "place",
                    "GET",
                    "https://geocoding-api.open-meteo.com/v1/search?count=1&name=${value.place}",
                ),
                _http(
                    "now",
                    "GET",
                    (
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
            "steps": [_command("say", "speak", text="${value.message}")],
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
                temperature=0 if attempt == 0 else RETRY_TEMPERATURE,
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
                            content=f"That plan was not accepted: {why.rstrip('.')}. Write the "
                            "plan again, fixing that.",
                        ),
                    ]
                )
        raise PlanError(f"the plan was not accepted: {why}")
