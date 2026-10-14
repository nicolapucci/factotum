"""Stand-ins for what the actions service depends on (a chat model, an embedding model, a runner,
the devices that commands go to, the clock), so that the tests of everything above them need no
network and no model."""

import asyncio
import re
import zlib
from collections.abc import AsyncGenerator, Sequence
from datetime import UTC, datetime, timedelta
from math import sqrt
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from factotum.actions.store import StoredSkill, StoredVersion, VersionMeta
from factotum.contracts.audio import AudioPayload, wav_of
from factotum.contracts.chat import (
    ChatRequest,
    ChatResult,
    EventUnion,
    FinishReason,
)
from factotum.contracts.commands import (
    AdjustVolume,
    CancelAlarm,
    CancelTimer,
    CommandResult,
    DeviceCommand,
    GetState,
    MoveAlarm,
    PostponeAlarm,
    SetAlarm,
    SetMute,
    SetTimer,
    SetVolume,
    StopRinging,
)
from factotum.contracts.effects import Effect
from factotum.contracts.identity import Owner, Subject
from factotum.contracts.ids import SkillId, TenantId
from factotum.contracts.models import ModelRole, Profile, StructuredOutputSupport
from factotum.contracts.skills import (
    ApprovalPolicy,
    ConfirmPolicy,
    ParameterSpec,
    ParamType,
    SkillManifest,
    SkillPermissions,
    SkillTarget,
)
from factotum.contracts.timekeeping import (
    AlarmInfo,
    AlarmsCancelled,
    AlarmSet,
    AlarmsMoved,
    AlarmsPostponed,
    RingsStopped,
    TimerInfo,
    TimersCancelled,
    TimerSet,
    TimeState,
    data_of,
)
from factotum.model_client import Embedded

if TYPE_CHECKING:
    from factotum.actions.core import CoreSkills

START = datetime(2026, 9, 21, 9, 30, tzinfo=UTC)


class Clock:
    """A clock that moves when it is told to, and by a ``tick`` each time it is read when it is
    told to do that: what is written one after another is then written in that order."""

    def __init__(self, now: datetime = START, *, tick: timedelta = timedelta(0)) -> None:
        self.current = now
        self._tick = tick

    def __call__(self) -> datetime:
        now = self.current
        self.current += self._tick
        return now

    def advance(self, **how_much: float) -> datetime:
        self.current += timedelta(**how_much)
        return self.current


class FakeSpeech:
    """The voice of a text, as the model gateway gives it, made up: a clip that carries the words
    (so that a test reads what a device was made to say) at a rate of its own. It keeps what it was
    asked, and what it is told to fail with (``failures``, in order, one for each ask) is raised.
    ``delay_s`` is how long each takes."""

    RATE = 22_050

    def __init__(self) -> None:
        self.asked: list[tuple[Owner, str]] = []
        self.failures: list[Exception | None] = []
        self.delay_s = 0.0

    async def synthesize(self, owner: Owner, text: str) -> AudioPayload:
        self.asked.append((owner, text))
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.failures:
            failure = self.failures.pop(0)
            if failure is not None:
                raise failure
        return self.clip_of(text)

    @classmethod
    def clip_of(cls, text: str) -> AudioPayload:
        """The clip that ``text`` is made into: its words, as the bytes of the samples."""
        pcm = text.encode("utf-8")
        return wav_of(pcm + b"\0" * (len(pcm) % 2), cls.RATE)

    @staticmethod
    def words_in(clip: AudioPayload) -> str:
        """The words that a clip made here carries."""
        from factotum.contracts.audio import pcm_of

        pcm, _ = pcm_of(clip.data)
        return pcm.rstrip(b"\0").decode("utf-8")


WALL = datetime(2026, 10, 13, 22, 1)  # noqa: DTZ001 - civil time: the wall has no zone
"""What the wall of a device shows in the tests: a Tuesday evening."""


def a_device_answers(command: DeviceCommand) -> CommandResult:
    """What a device that keeps time and a volume answers to a command, in the shape a skill reads:
    plausible, and not a device (the moment of an alarm is *tomorrow* whatever the day was said to
    be: working out the day is the device's, ``factotum.client_core.moments``). For the tests of
    the skills that read what a device answers, in place of one answer for every command."""
    tomorrow = WALL.replace(hour=7, minute=0) + timedelta(days=1)
    match command:
        case SetVolume():
            return CommandResult.ok({"volume": command.level})
        case AdjustVolume():
            return CommandResult.ok({"volume": 50})
        case SetMute():
            return CommandResult.ok({"muted": command.muted})
        case SetTimer():
            seconds = command.seconds
            timer = TimerInfo(id="t1", seconds=seconds, left_s=seconds)
            return CommandResult.ok(data_of(TimerSet(timer=timer)))
        case CancelTimer():
            timer = TimerInfo(id="t1", seconds=command.seconds or 300, left_s=60)
            return CommandResult.ok(data_of(TimersCancelled(cancelled=(timer,), remaining=0)))
        case SetAlarm():
            at = tomorrow.replace(hour=command.hour, minute=command.minute)
            alarm = AlarmInfo(id="a1", at=at)
            return CommandResult.ok(data_of(AlarmSet(now=WALL, alarm=alarm)))
        case CancelAlarm():
            at = tomorrow.replace(hour=command.hour or 7, minute=command.minute or 0)
            gone = AlarmsCancelled(now=WALL, cancelled=(AlarmInfo(id="a1", at=at),), remaining=0)
            return CommandResult.ok(data_of(gone))
        case PostponeAlarm():
            later = tomorrow + timedelta(seconds=command.seconds or 600)
            put_off = AlarmsPostponed(now=WALL, postponed=(AlarmInfo(id="a1", at=later),))
            return CommandResult.ok(data_of(put_off))
        case MoveAlarm():
            at = tomorrow.replace(hour=command.hour, minute=command.minute)
            moved = AlarmsMoved(now=WALL, moved=(AlarmInfo(id="a1", at=at),))
            return CommandResult.ok(data_of(moved))
        case StopRinging():
            ringing = AlarmInfo(id="a1", at=WALL, ringing=True)
            return CommandResult.ok(data_of(RingsStopped(now=WALL, alarms=(ringing,))))
        case GetState():
            state = TimeState(
                now=WALL,
                timers=(TimerInfo(id="t1", seconds=300, left_s=274),),
                alarms=(AlarmInfo(id="a1", at=tomorrow),),
            )
            return CommandResult.ok({"volume": 50, "muted": False, **data_of(state)})
    return CommandResult.ok()


def just_announce(speech: "FakeSpeech | None" = None) -> "CoreSkills":
    """The skills of the platform as they were when it had one: for the tests of what is true of
    any of them (how they are installed, enabled and left alone), which a second skill would only
    make longer to read. The tests of the others are theirs."""
    from factotum.actions.core import Announce, CoreSkills

    return CoreSkills(Announce(speech or FakeSpeech()))


class ScriptedChat:
    """A chat model that answers as told and keeps what it was asked.

    ``answers`` are what it says to the next requests for a JSON answer (a dict, or an error to
    raise), in order. A request nobody scripted is a failure of the test, not a made-up answer.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[Owner, ChatRequest]] = []
        self.answers: list[dict[str, Any] | Exception] = []

    def say(self, *answers: dict[str, Any] | Exception) -> None:
        self.answers.extend(answers)

    async def chat(self, owner: Owner, request: ChatRequest) -> ChatResult:
        self.requests.append((owner, request))
        if not self.answers:
            raise AssertionError("the model was asked something that the test did not expect")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return ChatResult(text=str(answer), parsed=answer, finish_reason=FinishReason.STOP)

    def chat_stream(self, owner: Owner, request: ChatRequest) -> AsyncGenerator[EventUnion]:
        raise NotImplementedError("the actions service asks for whole answers")

    async def profile(self, owner: Owner, role: ModelRole) -> Profile:
        return Profile(
            role=role,
            max_input_tokens=8192,
            max_output_tokens=1024,
            streaming=True,
            structured_output=StructuredOutputSupport.NATIVE,
        )

    @property
    def asked(self) -> list[ChatRequest]:
        return [request for _, request in self.requests]


_WORD = re.compile(r"[a-z0-9]+")
DIMENSIONS = 512
"""Many more than the words of a skill, since a word is a place and two words in the same place are
taken for one: with 64 of them an example of a skill that shared none of the words of a message
came near it by chance (0.47), as soon as the platform had enough skills."""


def embed_words(text: str) -> tuple[float, ...]:
    """A vector for a text, from its words: texts that share words are near, and texts that share
    none are not. Deterministic, and nothing like a real model, which is the point: what is tested
    is what the service does with the distances."""
    vector = [0.0] * DIMENSIONS
    for word in _WORD.findall(text.casefold()):
        vector[zlib.crc32(word.encode()) % DIMENSIONS] += 1.0
    norm = sqrt(sum(x * x for x in vector))
    return tuple(x / norm for x in vector) if norm else tuple(vector)


class FakeEmbedder:
    """An embedding model that is made of words, and can be told to fail or to change."""

    def __init__(self, space_id: str = "space-a") -> None:
        self.space_id = space_id
        self.calls: list[tuple[Owner, tuple[str, ...]]] = []
        self.failure: Exception | None = None
        self.delay_s = 0.0
        """How long it takes to answer: for a test of what is done when it takes too long."""

    async def embed(self, owner: Owner, texts: Sequence[str]) -> Embedded:
        self.calls.append((owner, tuple(texts)))
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.failure is not None:
            raise self.failure
        return Embedded(
            vectors=tuple(embed_words(text) for text in texts),
            space_id=self.space_id,
            dimensions=DIMENSIONS,
        )


# ---- skills, as they are stored, without going through a file

LAMP = SkillManifest(
    name="lamp",
    description="Turn a lamp on or off, or set how bright it is.",
    examples=("turn on the living room lamp", "switch off the lamp", "accendi la luce"),
    target=SkillTarget.EXTERNAL,
    parameters={
        "state": ParameterSpec(type=ParamType.STRING, enum=("on", "off")),
        "brightness": ParameterSpec(type=ParamType.INTEGER, minimum=0, maximum=100),
    },
    required=("state",),
    device_parameters={
        "entity_id": ParameterSpec(type=ParamType.STRING),
        "host": ParameterSpec(type=ParamType.STRING),
    },
    permissions=SkillPermissions(network=("${device.host}",), secrets=("HA_TOKEN",)),
    effect=Effect.REVERSIBLE,
    entrypoint="scripts/run.py",
)
GARAGE = SkillManifest(
    name="garage-door",
    description="Open or close the garage door.",
    examples=("open the garage door", "close the garage", "apri il garage"),
    target=SkillTarget.EXTERNAL,
    parameters={"action": ParameterSpec(type=ParamType.STRING, enum=("open", "close"))},
    required=("action",),
    device_parameters={
        "entity_id": ParameterSpec(type=ParamType.STRING),
        "host": ParameterSpec(type=ParamType.STRING),
    },
    permissions=SkillPermissions(network=("${device.host}",), secrets=("HA_TOKEN",)),
    effect=Effect.REVERSIBLE,
    confirm=ConfirmPolicy.ASK,
    entrypoint="scripts/run.py",
)
VOLUME = SkillManifest(
    name="volume",
    description="Set the volume of the device that is being spoken to.",
    examples=("set the volume to 30", "volume at fifty percent"),
    target=SkillTarget.SYSTEM,
    parameters={"level": ParameterSpec(type=ParamType.INTEGER, minimum=0, maximum=100)},
    required=("level",),
    effect=Effect.REVERSIBLE,
    entrypoint="scripts/run.py",
)
VOLUME_CHANGE = SkillManifest(
    name="volume-change",
    description="Turn the volume of the device that is being spoken to up or down.",
    examples=("turn the volume up", "turn the volume down by 20", "alza il volume"),
    target=SkillTarget.SYSTEM,
    parameters={
        "direction": ParameterSpec(type=ParamType.STRING, enum=("up", "down")),
        "amount": ParameterSpec(type=ParamType.INTEGER, minimum=1, maximum=100),
    },
    required=("direction",),
    effect=Effect.REVERSIBLE,
    entrypoint="scripts/run.py",
)
WEATHER = SkillManifest(
    name="weather",
    description="Say what the weather is like now in a city.",
    examples=("what is the weather like in Rome", "is it raining in Milan"),
    parameters={"place": ParameterSpec(type=ParamType.STRING)},
    required=("place",),
    permissions=SkillPermissions(network=("api.open-meteo.com",)),
    effect=Effect.READ_ONLY,
    entrypoint="scripts/run.py",
)
ROUTER = SkillManifest(
    name="restart-router",
    description="Restart the router of the home: everybody is disconnected for a while.",
    examples=("restart the router", "reboot the wifi"),
    permissions=SkillPermissions(network=("router.local",)),
    effect=Effect.REVERSIBLE,
    approval=ApprovalPolicy.REQUIRED,
    entrypoint="scripts/run.py",
)


# ---- the same skills, and one more, as instructions: no script, and notes for a model

LAMP_NOTES = (
    "Use the REST API of Home Assistant. To switch the lamp on, POST "
    "http://HOST:8123/api/services/light/turn_on (with brightness_pct when a brightness is asked "
    "for); to switch it off, POST http://HOST:8123/api/services/light/turn_off. Send "
    '{"entity_id": ENTITY} as JSON, with the header Authorization: Bearer TOKEN.'
)
LAMP_WORDS = LAMP.model_copy(update={"entrypoint": None})
"""The lamp, as instructions (it is bound to the same devices, under the same name)."""

GARAGE_NOTES = (
    "Use the REST API of Home Assistant: POST http://HOST:8123/api/services/cover/open_cover, or "
    'close_cover, with {"entity_id": ENTITY} as JSON and the header Authorization: Bearer TOKEN.'
)
GARAGE_WORDS = GARAGE.model_copy(update={"entrypoint": None})
"""The garage door, as instructions: it asks for a yes before it does anything."""

ANNOUNCE_NOTES = "Give the device a speak command with the message as its text."
ANNOUNCE = SkillManifest(
    name="announce",
    description="Say something out loud on a device.",
    examples=("tell the kitchen that dinner is ready", "announce that dinner is ready"),
    target=SkillTarget.SYSTEM,
    parameters={"message": ParameterSpec(type=ParamType.STRING)},
    required=("message",),
    effect=Effect.REVERSIBLE,
)
"""A skill that only has a device do something: no request, and no network."""


def stored_skill(
    manifest: SkillManifest,
    tenant: TenantId,
    *,
    owner: Owner | None = None,
    enabled: bool = True,
    version: int = 1,
    skill_id: SkillId | None = None,
) -> StoredSkill:
    """A skill as the store hands it out, owned by the tenant unless an owner is given, with its
    version enabled (with what it declares granted) unless it is not to be."""
    meta = VersionMeta(
        version=version,
        content_sha256=f"{version:064x}",
        created_at=START,
        enabled_at=START if enabled else None,
    )
    held = StoredVersion(
        meta=meta,
        manifest=manifest,
        instructions="",
        granted=manifest.permissions if enabled else None,
    )
    return StoredSkill(
        id=skill_id or SkillId(uuid4()),
        tenant_id=tenant,
        owner=owner or Owner.of_subject(Subject.tenant(tenant)),
        name=manifest.name,
        created_at=START,
        versions=(meta,),
        latest=held,
        enabled=held if enabled else None,
    )
