"""Choosing a skill for a message, and reading its values out of it.

A small model reads the message alone (never the conversation: what was said before cannot steer
it) together with the few skills that may be what was asked for, and says which one, on which
device, with which values, in which language, and what to ask the person if a yes or more
information is needed. It is asked in the ``chat.fast`` role, and the user chooses the model, so it
is asked in the way that made any small model get the gate right (``conversation.gate``): the
examples are turns of the conversation the model is in, not a list in the instructions, and the
answer says *why*, in a few words, before it says which.

What the model says is a proposal and nothing more. That the skill is one of those offered, that the
device is one the caller may use, that the values are what the skill declares: all of it is checked
in code afterwards (``understanding``), and what is refused there is never run, however sure the
model was.
"""

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any

from pydantic import Field, ValidationError, field_validator

from factotum.contracts.base import Contract
from factotum.contracts.chat import ChatMessage, ChatRequest, MessageRole
from factotum.contracts.devices import ExternalDeviceRecord, SystemDeviceRecord
from factotum.contracts.errors import GatewayError, ProviderTimeoutError
from factotum.contracts.identity import Owner
from factotum.contracts.models import ModelRole
from factotum.contracts.skills import ParameterSpec, ParamType, SkillManifest, SkillTarget
from factotum.model_client import Chat

MAX_CHARS = 600
"""How much of a message is read. A long one is cut in the middle: what asks for something is said
at either end, and the point of this call is to be quick."""

_LANGUAGE_NAMES = {
    "english": "en",
    "italian": "it",
    "italiano": "it",
    "inglese": "en",
    "french": "fr",
    "spanish": "es",
    "german": "de",
}


class Plan(Contract):
    """What the model is asked to answer. The lengths here only keep a runaway answer out: what a
    field may say is decided by what is done with it (``understanding``), not by refusing a plan
    whose ``question`` is a little long."""

    reason: str = Field(max_length=500)
    """Why, in a few words: the answer that comes first is the one a small model gets right."""

    skill: str | None = Field(default=None, max_length=500)
    """The name of one of the skills it was shown, or nothing when the message asks for none."""

    device: str | None = Field(default=None, max_length=500)
    """The device the message means, as it is named in the list, when it names one."""

    parameters: dict[str, Any] = Field(default_factory=dict)
    """The values the message gives, by the names the skill declares."""

    language: str = Field(default="en", max_length=64)
    """The language of the message."""

    question: str | None = Field(default=None, max_length=1000)
    """The yes or no question to ask before doing it, in the language of the message, if it has to
    be asked."""

    ask: str | None = Field(default=None, max_length=1000)
    """What to ask when a value is missing, in the language of the message."""

    @field_validator("language", mode="before")
    @classmethod
    def _a_language_code(cls, value: Any) -> str:
        """A code of two or three letters, whatever the model wrote ("Italian", "IT", "it-IT")."""
        text = str(value or "en").strip().lower()
        text = _LANGUAGE_NAMES.get(text, text)
        match = re.fullmatch(r"([a-z]{2,3})(?:[-_][a-z0-9]{2,8})?", text)
        return match.group(1) if match else "en"


RESPONSE_SCHEMA: dict[str, Any] = Plan.model_json_schema()
"""What a model is asked to answer with: the shape of ``Plan``."""


SYSTEM_PROMPT = """\
You are the part of a voice assistant that decides whether a message asks for something to be \
done with one of the skills it is shown, and if so which one and with which values. You are not \
shown the conversation. Answer with JSON only.

"reason" is a few words on why.

"skill" is the name of the skill the message asks for, or null. Only a message that asks for \
something to be done now is an action: a question, a request for information from documents, \
a question about how to do something, chat or thanks is not, even if it mentions a skill's words. \
If no skill fits, or none is shown for it, it is null.

"device" is the device the message means, written as it is in the skill's list, or null when the \
message does not name one (for a skill that acts on the device that is spoken to, that is null).

"parameters" has the values the message gives, by the names the skill declares, with the types it \
declares. Leave out the ones the message does not give. Never make one up.

"language" is the language of the message as a two-letter code.

"question" is a short yes or no question that says exactly what will be done, in the language of \
the message, to ask before doing it. "ask" is a short question to get a value that is missing, \
in the language of the message; null when nothing is missing.\
"""


@dataclass(frozen=True, slots=True)
class SkillView:
    """A skill as the model is shown it."""

    name: str
    description: str
    target: SkillTarget
    values: tuple[str, ...] = ()
    devices: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PlanDevices:
    """The devices the model is shown."""

    here: str | None = None
    """The device that was spoken to."""

    others: tuple[str, ...] = ()
    """The other system devices the caller may use."""


def _value(name: str, spec: ParameterSpec, required: bool) -> str:
    if spec.enum is not None:
        kind = " or ".join(str(choice) for choice in spec.enum)
    else:
        kind = {
            ParamType.STRING: "text",
            ParamType.INTEGER: "whole number",
            ParamType.NUMBER: "number",
            ParamType.BOOLEAN: "true or false",
        }[spec.type]
        if spec.minimum is not None and spec.maximum is not None:
            kind += f" {spec.minimum:g} to {spec.maximum:g}"
    shown = f"{name} ({kind}{', required' if required else ''})"
    return f"{shown}: {spec.description}" if spec.description else shown


def view_of(
    manifest: SkillManifest,
    devices: list[SystemDeviceRecord | ExternalDeviceRecord] | None = None,
) -> SkillView:
    """A skill for the model: what it is for, the values it takes, the devices it acts on."""
    names = tuple(
        device.name + (f" (also called: {', '.join(device.aliases)})" if device.aliases else "")
        for device in devices or []
    )
    return SkillView(
        name=manifest.name,
        description=manifest.description,
        target=manifest.target,
        values=tuple(
            _value(name, spec, name in manifest.required)
            for name, spec in manifest.parameters.items()
        ),
        devices=names,
    )


def render(skills: list[SkillView], devices: PlanDevices, message: str) -> str:
    """One turn of what the model is asked: the skills that may fit, and the message."""
    lines = ["Skills:"]
    for skill in skills:
        lines.append(f"- {skill.name}: {skill.description}")
        if skill.values:
            lines.append(f"  values: {'; '.join(skill.values)}")
        if skill.target is SkillTarget.EXTERNAL and skill.devices:
            lines.append(f"  devices: {'; '.join(skill.devices)}")
        if skill.target is SkillTarget.SYSTEM:
            lines.append("  acts on: the device that is spoken to, unless another is named")
    if devices.here is not None:
        lines.append(f"The device that is spoken to: {devices.here}")
    if devices.others:
        lines.append(f"Other devices: {'; '.join(devices.others)}")
    lines.append(f"Message: {shortened(message)}")
    return "\n".join(lines)


def shortened(message: str) -> str:
    """What of a message the model reads: all of it, or its two ends."""
    if len(message) <= MAX_CHARS:
        return message
    half = MAX_CHARS // 2
    return f"{message[:half].rstrip()} … {message[-half:].lstrip()}"


@dataclass(frozen=True, slots=True)
class Example:
    """A message, the skills it was shown with, and how it is read."""

    message: str
    skills: tuple[SkillView, ...]
    plan: dict[str, Any]
    devices: PlanDevices = PlanDevices(here="Kitchen speaker")

    def user(self) -> str:
        return render(list(self.skills), self.devices, self.message)

    def answer(self) -> str:
        return json.dumps(self.plan, ensure_ascii=False)


# The skills of the examples are shown as the ones that come with the project are, with what each
# value is for (a test keeps the two the same): what is shown of a real skill is what the model has
# been shown how to read.
_LAMP = SkillView(
    name="lamp",
    description="Turn a lamp on or off, or set how bright it is.",
    target=SkillTarget.EXTERNAL,
    values=(
        "state (on or off, required): whether the lamp is to be on or off (on, when only a "
        "brightness is asked for)",
        "brightness (whole number 0 to 100): how bright it is to be, in percent",
    ),
    devices=("Living room lamp (also called: lamp, the lamp)", "Bedroom lamp"),
)
_WEATHER = SkillView(
    name="weather",
    description="Say what the weather is like now in a city.",
    target=SkillTarget.NONE,
    values=("place (text, required): the city, as the person said it",),
)
_VOLUME = SkillView(
    name="volume",
    description="Set the volume of the device that is being spoken to.",
    target=SkillTarget.SYSTEM,
    values=("level (whole number 0 to 100, required): the volume, in percent",),
)
_GARAGE = SkillView(
    name="garage-door",
    description="Open or close the garage door.",
    target=SkillTarget.EXTERNAL,
    values=("action (open or close, required): whether the door is to open or to close",),
    devices=("Garage door (also called: garage)",),
)


def _plan(reason: str, **fields: Any) -> dict[str, Any]:
    return {
        "reason": reason,
        "skill": None,
        "device": None,
        "parameters": {},
        "language": "en",
        "question": None,
        "ask": None,
        **fields,
    }


EXAMPLES: tuple[Example, ...] = (
    Example(
        "Turn on the living room lamp",
        (_LAMP,),
        _plan(
            "asks to turn a lamp on",
            skill="lamp",
            device="Living room lamp",
            parameters={"state": "on"},
            question="Do you want me to turn on the living room lamp?",
        ),
    ),
    Example(
        "What does the warranty say about water damage?",
        (_LAMP, _VOLUME),
        _plan("asks about a document"),
    ),
    Example(
        "Spegni la lampada della camera",
        (_LAMP,),
        _plan(
            "asks to turn off a lamp",
            skill="lamp",
            device="Bedroom lamp",
            parameters={"state": "off"},
            language="it",
            question="Vuoi che spenga la lampada della camera?",
        ),
    ),
    Example(
        "Set the volume to 30 percent",
        (_VOLUME, _WEATHER),
        _plan(
            "asks to set the volume",
            skill="volume",
            parameters={"level": 30},
            question="Do you want me to set the volume to 30?",
        ),
    ),
    Example(
        "How do I turn on the lamp from the app?",
        (_LAMP,),
        _plan("asks how to do it, not to do it"),
    ),
    Example(
        "What's the weather like?",
        (_WEATHER,),
        _plan(
            "asks for the weather but not where",
            skill="weather",
            ask="Which city do you want the weather for?",
        ),
    ),
    Example(
        "Apri il garage",
        (_GARAGE, _LAMP),
        _plan(
            "asks to open the garage door",
            skill="garage-door",
            device="Garage door",
            parameters={"action": "open"},
            language="it",
            question="Vuoi davvero che apra il garage?",
        ),
    ),
    Example("Play some jazz", (_LAMP, _VOLUME), _plan("asks for music, which no skill does")),
    Example(
        "Che tempo fa a Torino?",
        (_WEATHER,),
        _plan(
            "asks the weather in a city",
            skill="weather",
            parameters={"place": "Torino"},
            language="it",
            question="Vuoi sapere che tempo fa a Torino?",
        ),
    ),
    Example("Tell me a joke", (_WEATHER,), _plan("asks for a joke")),
    Example(
        "Set the lamp to 40 percent",
        (_LAMP,),
        _plan(
            "asks for a lamp at 40 percent without saying which",
            skill="lamp",
            parameters={"state": "on", "brightness": 40},
            question="Do you want me to set the lamp to 40 percent?",
        ),
    ),
    Example(
        "Close the garage door",
        (_GARAGE,),
        _plan(
            "asks to close the garage door",
            skill="garage-door",
            device="Garage door",
            parameters={"action": "close"},
            question="Do you want me to close the garage door?",
        ),
    ),
)


def _shown() -> tuple[ChatMessage, ...]:
    turns: list[ChatMessage] = []
    for example in EXAMPLES:
        turns.append(ChatMessage(role=MessageRole.USER, content=example.user()))
        turns.append(ChatMessage(role=MessageRole.ASSISTANT, content=example.answer()))
    return tuple(turns)


_SHOWN = _shown()


class Planner:
    def __init__(self, chat: Chat, *, timeout_s: float = 10.0) -> None:
        self._chat = chat
        self._timeout_s = timeout_s

    async def plan(
        self, owner: Owner, message: str, views: list[SkillView], devices: PlanDevices
    ) -> Plan:
        """What the message asks for, as the model reads it. Raises a ``FactotumError`` when it
        cannot be told: the gateway did not answer, the model did not answer in time or with
        something that is not a plan."""
        request = ChatRequest(
            role=ModelRole.CHAT_FAST,
            messages=(
                ChatMessage(role=MessageRole.SYSTEM, content=SYSTEM_PROMPT),
                *_SHOWN,
                ChatMessage(role=MessageRole.USER, content=render(views, devices, message)),
            ),
            response_schema=RESPONSE_SCHEMA,
            temperature=0,
            max_output_tokens=300,
        )
        try:
            async with asyncio.timeout(self._timeout_s):
                result = await self._chat.chat(owner, request)
        except TimeoutError:
            raise ProviderTimeoutError("the model did not decide in time") from None
        try:
            return Plan.model_validate(result.parsed)
        except ValidationError:
            raise GatewayError("the model's answer is not a plan") from None
