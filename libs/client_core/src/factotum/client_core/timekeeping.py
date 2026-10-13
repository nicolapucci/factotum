"""Timers and alarms: what a device keeps of time, on a clock of its own (ADR 0029).

A skill of the platform tells a device to ring after five minutes or at seven tomorrow, and the
device does it: it keeps the time itself, so it rings when the moment comes even if its connection
to the edge is down then, and it is the one place that knows what time it is where it is (the
platform has no clock, and nothing in it works one out). What rings is a clip that the platform made
when the timer was set, in the voice of its tenant; the device says it when nothing else is being
said, and says it again every few seconds until a person stops it, or for as long as
``ring_for_s``.

A timer is kept on the clock that only goes forward (the wall may be set to another hour while it
runs) and an alarm on the wall (``moments``: it rings at seven on the clock of the place, whatever
that clock did in between). What a device holds is lost when its program stops, which the first
version says plainly rather than pretends otherwise, and nothing repeats: an alarm is for one day.

The keeper is the device's and not a connection's: a client comes and goes with the connection,
the keeper stays, and the client that is connected lends it its way of saying a clip (``attach``)
so that a ring waits for an answer to end, and a person who speaks cuts it. With no client the
keeper says it on the speaker itself.
"""

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from pydantic import JsonValue

from factotum.client_core.audio import VoiceOutput
from factotum.client_core.moments import next_moment
from factotum.client_core.ports import Renderer
from factotum.contracts.audio import AudioPayload
from factotum.contracts.commands import (
    CancelAlarm,
    CancelTimer,
    CommandResult,
    SetAlarm,
    SetTimer,
)
from factotum.contracts.timekeeping import (
    MAX_ALARMS,
    MAX_TIMERS,
    AlarmInfo,
    AlarmsCancelled,
    AlarmSet,
    TimerInfo,
    TimersCancelled,
    TimerSet,
    TimeState,
    data_of,
)

logger = logging.getLogger("factotum.client_core")

TimeCommand = SetTimer | CancelTimer | SetAlarm | CancelAlarm
"""What the keeper does: the commands about time."""

RING_EVERY_S = 8.0
"""How long after a ring has been said it is said again, while it is not stopped."""

RING_FOR_S = 180.0
"""How long a timer or an alarm rings, if nobody stops it: a device in an empty house does not ring
for ever."""

TICK_S = 1.0
"""The most that the keeper sleeps: it looks at both clocks at least this often, so that a wall that
was set to another hour is noticed."""

_LEAST_WAIT_S = 0.01


class Clock(Protocol):
    """The clock of the device: the only one that is asked what time it is."""

    def wall(self) -> datetime:
        """What the wall of the place shows (civil time, with no zone)."""
        ...

    def monotonic(self) -> float:
        """Seconds on a clock that only goes forward: how long a timer has run."""
        ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """The clock of the machine, in the time zone it is set to."""

    def wall(self) -> datetime:
        return datetime.now(UTC).astimezone().replace(tzinfo=None, microsecond=0)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass(frozen=True, slots=True)
class Ring:
    """What a timer or an alarm says when it is due: the words, and the clip of them."""

    text: str
    audio: AudioPayload


Sayer = Callable[[Ring], Awaitable[None]]
"""How a ring is said when nothing else is: it returns when the clip has been said, or cut."""


@dataclass(slots=True, eq=False, kw_only=True)
class _Item:
    """A timer or an alarm: what it says, and whether it is ringing."""

    id: str
    ring: Ring
    ringing_since: float | None = None
    """When it began to ring, on the clock that only goes forward."""

    next_ring: float = 0.0
    """When it is to be said again."""


@dataclass(slots=True, eq=False, kw_only=True)
class _Timer(_Item):
    seconds: int
    due: float
    """When it is up, on the clock that only goes forward."""


@dataclass(slots=True, eq=False, kw_only=True)
class _Alarm(_Item):
    at: datetime
    """When it rings, on the wall."""


class Timekeeper:
    """The timers and the alarms of one device, and the task that rings them."""

    def __init__(
        self,
        renderer: Renderer,
        *,
        clock: Clock | None = None,
        output: VoiceOutput | None = None,
        ring_every_s: float = RING_EVERY_S,
        ring_for_s: float = RING_FOR_S,
    ) -> None:
        self._renderer = renderer
        self._clock: Clock = clock or SystemClock()
        self._output = output
        self._ring_every_s = ring_every_s
        self._ring_for_s = ring_for_s
        self._sayer: Sayer | None = None
        self._timers: list[_Timer] = []
        self._alarms: list[_Alarm] = []
        self._counter = 0
        self._task: asyncio.Task[None] | None = None
        self._saying: tuple[_Item, asyncio.Task[None]] | None = None
        """The ring that is being said, and the task that says it."""

    # ---- the way a ring is said --------------------------------------------------------------

    def attach(self, sayer: Sayer) -> None:
        """Say rings the way a connected client does, which waits for the answer it is giving to
        end and is cut by a person who speaks."""
        self._sayer = sayer

    def detach(self, sayer: Sayer) -> None:
        """The client is gone (its connection ended): rings are said on the speaker itself. A
        client that was already replaced by another is not detached by being left late."""
        if self._sayer == sayer:
            self._sayer = None

    # ---- what the device is told ---------------------------------------------------------------

    async def execute(self, command: TimeCommand) -> CommandResult:
        """Do a command about time, and answer with what became of it."""
        match command:
            case SetTimer():
                return self._set_timer(command)
            case CancelTimer():
                return self._cancel_timer(command)
            case SetAlarm():
                return self._set_alarm(command)
            case CancelAlarm():
                return self._cancel_alarm(command)

    def state(self) -> dict[str, JsonValue]:
        """How the device is, as far as time goes: what the wall shows, and what it holds."""
        mono = self._clock.monotonic()
        return data_of(
            TimeState(
                now=self._clock.wall(),
                timers=tuple(self._info_of(timer, mono) for timer in self._by_due()),
                alarms=tuple(self._alarm_info(alarm) for alarm in self._by_moment()),
            )
        )

    async def close(self) -> None:
        """Nothing is kept or rung any more: the program of the device is over."""
        self._timers.clear()
        self._alarms.clear()
        saying, self._saying = self._saying, None
        tasks = [task for task in (self._task, saying[1] if saying else None) if task is not None]
        self._task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks)

    # ---- timers ----------------------------------------------------------------------------------

    def _set_timer(self, command: SetTimer) -> CommandResult:
        if len(self._timers) >= MAX_TIMERS:
            return CommandResult.ok(data_of(TimerSet(reason="too_many")))
        mono = self._clock.monotonic()
        timer = _Timer(
            id=self._new_id("t"),
            ring=Ring(command.text, command.audio),
            seconds=command.seconds,
            due=mono + command.seconds,
        )
        self._timers.append(timer)
        self._renderer.event(f"timer {timer.id} set for {timer.seconds} s")
        self._keep_time()
        return CommandResult.ok(data_of(TimerSet(timer=self._info_of(timer, mono))))

    def _cancel_timer(self, command: CancelTimer) -> CommandResult:
        """The timer named by its length, else the ones that ring, else the next to ring."""
        named = [t for t in self._by_due() if command.seconds in (None, t.seconds)]
        chosen = [timer for timer in named if timer.ringing_since is not None] or named[:1]
        mono = self._clock.monotonic()
        info = tuple(self._info_of(timer, mono) for timer in chosen)
        for timer in chosen:
            self._timers.remove(timer)
            self._stop(timer)
            self._renderer.event(f"timer {timer.id} cancelled")
        return CommandResult.ok(
            data_of(TimersCancelled(cancelled=info, remaining=len(self._timers)))
        )

    # ---- alarms ----------------------------------------------------------------------------------

    def _set_alarm(self, command: SetAlarm) -> CommandResult:
        wall = self._clock.wall()
        if len(self._alarms) >= MAX_ALARMS:
            return CommandResult.ok(data_of(AlarmSet(now=wall, reason="too_many")))
        moment = next_moment(wall, command.hour, command.minute, command.day)
        if isinstance(moment, str):
            return CommandResult.ok(data_of(AlarmSet(now=wall, reason=moment)))
        alarm = _Alarm(id=self._new_id("a"), ring=Ring(command.text, command.audio), at=moment)
        self._alarms.append(alarm)
        self._renderer.event(f"alarm {alarm.id} set for {moment:%Y-%m-%d %H:%M}")
        self._keep_time()
        return CommandResult.ok(data_of(AlarmSet(now=wall, alarm=self._alarm_info(alarm))))

    def _cancel_alarm(self, command: CancelAlarm) -> CommandResult:
        """The alarm named by its hour, else the ones that ring, else the next to ring."""
        named = [
            alarm
            for alarm in self._by_moment()
            if command.hour is None
            or (alarm.at.hour, alarm.at.minute) == (command.hour, command.minute)
        ]
        chosen = [alarm for alarm in named if alarm.ringing_since is not None] or named[:1]
        info = tuple(self._alarm_info(alarm) for alarm in chosen)
        for alarm in chosen:
            self._alarms.remove(alarm)
            self._stop(alarm)
            self._renderer.event(f"alarm {alarm.id} cancelled")
        answer = AlarmsCancelled(
            now=self._clock.wall(), cancelled=info, remaining=len(self._alarms)
        )
        return CommandResult.ok(data_of(answer))

    # ---- what is kept and what is shown of it ---------------------------------------------------

    def _new_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter}"

    def _by_due(self) -> list[_Timer]:
        return sorted(self._timers, key=lambda timer: timer.due)

    def _by_moment(self) -> list[_Alarm]:
        return sorted(self._alarms, key=lambda alarm: alarm.at)

    def _info_of(self, timer: _Timer, mono: float) -> TimerInfo:
        ringing = timer.ringing_since is not None
        left = 0 if ringing else max(0, math.ceil(timer.due - mono))
        return TimerInfo(id=timer.id, seconds=timer.seconds, left_s=left, ringing=ringing)

    def _alarm_info(self, alarm: _Alarm) -> AlarmInfo:
        return AlarmInfo(id=alarm.id, at=alarm.at, ringing=alarm.ringing_since is not None)

    def _stop(self, item: _Item) -> None:
        """A ring that is being said for an item that is gone is cut."""
        saying = self._saying
        if saying is not None and saying[0] is item:
            saying[1].cancel()

    # ---- keeping the time ------------------------------------------------------------

    def _keep_time(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="timekeeper")

    async def _run(self) -> None:
        """Look at the clocks, ring what is due, and sleep until there is something to look at."""
        while self._timers or self._alarms:
            try:
                self._look()
            except Exception:
                logger.exception("the timekeeper could not look at the clock")
                self._renderer.notice("the timers could not be looked at: see the log")
            if self._timers or self._alarms:  # what was just given up on is not waited for
                await self._clock.sleep(self._wait_s())

    def _look(self) -> None:
        """Start what is due, give up on what has rung for long enough, and say what is next."""
        mono, wall = self._clock.monotonic(), self._clock.wall()
        for timer in self._timers:
            if timer.ringing_since is None and timer.due <= mono:
                self._ring(timer, mono, f"timer {timer.id} is up")
        for alarm in self._alarms:
            if alarm.ringing_since is None and alarm.at <= wall:
                self._ring(alarm, mono, f"alarm {alarm.id} is ringing")
        for item in self._ringing():
            if item.ringing_since is not None and mono - item.ringing_since >= self._ring_for_s:
                self._give_up(item)
        self._say_next(mono)

    def _ringing(self) -> list[_Item]:
        return [item for item in (*self._timers, *self._alarms) if item.ringing_since is not None]

    def _ring(self, item: _Item, mono: float, what: str) -> None:
        item.ringing_since, item.next_ring = mono, mono
        self._renderer.event(what)

    def _give_up(self, item: _Item) -> None:
        """Nobody stopped it: it stops, and the device says that it did."""
        if isinstance(item, _Timer):
            self._timers.remove(item)
        elif isinstance(item, _Alarm):
            self._alarms.remove(item)
        self._stop(item)
        self._renderer.notice(f"{item.id} rang for {self._ring_for_s:.0f} s and nobody stopped it")

    def _say_next(self, mono: float) -> None:
        """Say the ring that was due first, when none is being said: one at a time, so that two
        that are up together are two sentences and not one on top of the other."""
        if self._saying is not None and not self._saying[1].done():
            return
        due = [item for item in self._ringing() if item.next_ring <= mono]
        if due:
            item = min(due, key=lambda ringing: ringing.next_ring)
            self._saying = (item, asyncio.create_task(self._say(item), name="ring"))

    async def _say(self, item: _Item) -> None:
        try:
            if self._sayer is not None:
                await self._sayer(item.ring)
            elif self._output is not None:
                await self._output.play(item.ring.audio)
            else:
                self._renderer.speech(item.ring.text)
        except Exception as error:  # noqa: BLE001 - whatever a speaker fails with, it is tried again
            self._renderer.notice(f"a ring could not be said: {error}")
        finally:
            item.next_ring = self._clock.monotonic() + self._ring_every_s

    def _wait_s(self) -> float:
        """How long to sleep: until the next thing that is due, and no more than a tick."""
        mono, wall = self._clock.monotonic(), self._clock.wall()
        waits = [TICK_S]
        saying = self._saying is not None and not self._saying[1].done()
        waits.extend(timer.due - mono for timer in self._timers if timer.ringing_since is None)
        waits.extend(
            (alarm.at - wall).total_seconds()
            for alarm in self._alarms
            if alarm.ringing_since is None
        )
        for item in self._ringing():
            if item.ringing_since is not None:
                waits.append(item.ringing_since + self._ring_for_s - mono)
                if not saying:
                    waits.append(item.next_ring - mono)
        return max(_LEAST_WAIT_S, min(waits))
