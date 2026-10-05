"""The sound of an answer, played as it arrives (ADR 0023).

The edge sends the sound faster than it plays, a frame at a time, and a client that played each
frame where it reads it would not read the next until the speaker had taken this one: a person who
speaks over the answer, a command to run and a ping to be answered would all wait for the sound. So
what is received is put in a queue and played by a task of its own, and what is received for an
answer that is spoken over is dropped, and what the speaker is holding is given up on, at once.
"""

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass

from factotum.client_core.audio import MarkedSink, VoiceSink


@dataclass(frozen=True, slots=True)
class _Begin:
    sample_rate: int


@dataclass(frozen=True, slots=True)
class _Write:
    pcm: bytes
    part_seq: int


@dataclass(frozen=True, slots=True)
class _End:
    pass


_Item = _Begin | _Write | _End


class VoicePlayer:
    """Gives a speaker the tracks of sound that a client receives, in order, and lets go of them
    when the person speaks over the answer.

    A speaker that fails (it was unplugged, it is busy) loses the track it was playing and is tried
    again at the next one: what a client says of it is ``on_failure``, and the answer goes on.
    """

    def __init__(self, sink: VoiceSink, on_failure: Callable[[str], None]) -> None:
        self._sink = sink
        self._on_failure = on_failure
        self._queue: asyncio.Queue[_Item] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._unplayed = 0
        """What was given and has not been played, or given up on, yet."""

        self._open = False
        """A track has begun and has not ended: the speaker is playing what it was given of it
        (and what comes next is on its way), whether or not something is queued."""

        self._idle = asyncio.Event()
        self._idle.set()
        self._broken = False
        """The speaker failed in the track that is going on: the rest of it is not given."""

        self._part: int | None = None
        """The sentence that the speaker was last told it was playing (``MarkedSink``)."""

    @property
    def playing(self) -> bool:
        """Whether there is sound that has been received and has not been played: a track that has
        begun and has not ended is being played, even when what was given of it has been taken."""
        return not self._idle.is_set()

    def begin(self, sample_rate: int) -> None:
        """A track of sound begins."""
        self._give(_Begin(sample_rate))

    def write(self, pcm: bytes, part_seq: int = 0) -> None:
        """The next samples of the track, of the sentence whose ``seq`` is ``part_seq``."""
        self._give(_Write(pcm, part_seq))

    def end(self) -> None:
        """The track has no more: what was given is played to its end."""
        self._give(_End())

    async def idle(self) -> None:
        """Wait until everything that was given has been played, or given up on."""
        await self._idle.wait()

    async def stop(self) -> None:
        """Stop now. What was given and not played is dropped and the speaker is told to stop: a
        person is speaking over the answer, or has said to stop it."""
        was_playing = self.playing
        worker, self._worker = self._worker, None
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.wait({worker})
        while not self._queue.empty():
            self._queue.get_nowait()
        self._unplayed = 0
        self._open = False
        self._broken = False
        self._part = None
        if was_playing:
            with contextlib.suppress(Exception):  # a speaker that cannot stop has stopped
                await self._sink.abort()
        self._idle.set()

    def _give(self, item: _Item) -> None:
        self._unplayed += 1
        if isinstance(item, _Begin):
            self._open = True
        self._idle.clear()
        self._queue.put_nowait(item)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="voice-player")

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            await self._play(item)  # a cancel (stop) ends the task: stop sets the counts right
            self._unplayed -= 1
            if isinstance(item, _End):
                self._open = False
            if self._unplayed == 0 and not self._open:
                self._idle.set()

    async def _play(self, item: _Item) -> None:
        if isinstance(item, _Begin):
            self._broken = False
            self._part = None
        elif self._broken:
            return
        try:
            match item:
                case _Begin():
                    await self._sink.begin(item.sample_rate)
                case _Write():
                    if item.part_seq != self._part and isinstance(self._sink, MarkedSink):
                        self._part = item.part_seq
                        await self._sink.mark(item.part_seq)
                    await self._sink.write(item.pcm)
                case _End():
                    await self._sink.finish()
        except Exception as error:  # noqa: BLE001 - whatever a speaker fails with, the answer goes on
            self._broken = True
            self._on_failure(f"the speaker could not play the sound: {error}")
