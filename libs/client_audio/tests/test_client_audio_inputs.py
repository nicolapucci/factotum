"""The two ways a person gets a device to listen, with a sound card that a test plays: a key, and a
word. What each hands over is what a person says, as they say it."""

import asyncio
import queue
import threading
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any, TextIO, cast

import numpy as np
import pytest

from factotum.client_audio.endpoint import capture_speech
from factotum.client_audio.inputs import PushToTalk, WakeWord
from factotum.client_audio.keys import EnterKey
from factotum.client_audio.microphone import Microphone
from factotum.client_audio.testing import FakeBackend, KeyPresses
from factotum.client_audio.wakeword import WakeWordError

RATE = 16_000
FRAME = 512


def loud(level: int = 1000) -> Any:
    return np.full(FRAME, level, dtype=np.int16)


def quiet() -> Any:
    return np.zeros(FRAME, dtype=np.int16)


async def said(stream_of: Any, backend: FakeBackend, frames: list[Any]) -> bytes:
    """What an ``AudioStream`` carries while a test says ``frames`` to the microphone."""
    chunks: list[bytes] = []

    async def read() -> None:
        chunks.extend([chunk async for chunk in stream_of.chunks])

    reading = asyncio.create_task(read())
    for frame in frames:
        await asyncio.sleep(0)
        backend.hearing.hear(frame)
    await asyncio.wait_for(reading, 2)
    return b"".join(chunks)


def watching_what_listens(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """What listening does when it is closed, which is all that shows that a stream that nobody
    reads to its end was closed with it (and not later, whenever the interpreter gets to it)."""
    closed: list[str] = []

    async def watched(*arguments: Any, **keywords: Any) -> AsyncGenerator[bytes]:
        try:
            async for chunk in capture_speech(*arguments, **keywords):
                yield chunk
        finally:
            closed.append("closed")

    monkeypatch.setattr("factotum.client_audio.inputs.capture_speech", watched)
    return closed


class Typing:
    """What a person types, a line when a test says: a read waits for it, as a terminal's does."""

    def __init__(self) -> None:
        self._lines: queue.Queue[str] = queue.Queue()

    def type(self, line: str = "\n") -> None:
        self._lines.put(line)

    def end(self) -> None:
        self._lines.put("")

    def readline(self) -> str:
        return self._lines.get()


class TestAKey:
    def push_to_talk(
        self, presses: KeyPresses, backend: FakeBackend, **changes: Any
    ) -> tuple[PushToTalk, list[str]]:
        told: list[str] = []
        fields: dict[str, Any] = {
            "silence_seconds": 0.2,
            "silence_level": 150,
            "max_seconds": 5.0,
            "on_listening": lambda: told.append("listening"),
        }
        microphone = Microphone(backend, sample_rate=RATE, block_size=FRAME)
        return PushToTalk(presses, microphone, **{**fields, **changes}), told

    async def test_a_press_opens_the_microphone_and_what_follows_is_what_is_streamed(self) -> None:
        backend, presses = FakeBackend(), KeyPresses()
        talk, told = self.push_to_talk(presses, backend)
        listening = talk.listen()
        presses.press()

        stream = await asyncio.wait_for(anext(listening), 2)
        pcm = await said(stream, backend, [loud(1), loud(2), loud(3)] + [quiet()] * 8)

        assert stream.sample_rate == RATE
        assert told == ["listening"]
        assert np.frombuffer(pcm, dtype="<i2")[::FRAME].tolist()[:3] == [1, 2, 3]
        await listening.aclose()

    async def test_the_microphone_is_open_for_what_is_said_and_not_after(self) -> None:
        backend, presses = FakeBackend(), KeyPresses()
        talk, _ = self.push_to_talk(presses, backend)
        listening = talk.listen()

        assert backend.inputs == []  # nothing is heard that was not asked to be
        presses.press()
        stream = await asyncio.wait_for(anext(listening), 2)
        assert not backend.hearing.closed
        await said(stream, backend, [loud()] + [quiet()] * 8)
        presses.press()
        presses.leave()
        with_next = asyncio.ensure_future(anext(listening, None))
        await asyncio.sleep(0.05)

        assert backend.inputs[0].closed  # closed as soon as the next press was asked for
        await with_next

    async def test_each_press_is_a_microphone_of_its_own(self) -> None:
        backend, presses = FakeBackend(), KeyPresses()
        talk, told = self.push_to_talk(presses, backend)
        listening = talk.listen()

        for _ in range(2):
            presses.press()
            stream = await asyncio.wait_for(anext(listening), 2)
            await said(stream, backend, [loud()] + [quiet()] * 8)

        assert len(backend.inputs) == 2
        assert told == ["listening", "listening"]
        await listening.aclose()

    async def test_a_person_who_presses_again_has_said_they_are_done(self) -> None:
        backend, presses = FakeBackend(), KeyPresses()
        talk, _ = self.push_to_talk(presses, backend, silence_seconds=5.0)
        listening = talk.listen()
        presses.press()
        stream = await asyncio.wait_for(anext(listening), 2)

        async def say_and_press() -> bytes:
            reading = asyncio.create_task(said(stream, backend, [loud()] * 3))
            await asyncio.sleep(0.01)
            presses.again = True  # the key, pressed again while they were speaking
            backend.hearing.hear(loud())
            return await reading

        pcm = await asyncio.wait_for(say_and_press(), 2)

        assert len(pcm) // 2 // FRAME == 4  # up to the frame that was taken when they pressed
        await listening.aclose()

    async def test_a_press_that_came_before_it_began_is_not_taken_for_ending_it(self) -> None:
        backend, presses = FakeBackend(), KeyPresses()
        presses.again = True  # the key was pressed twice, and the second belongs to the first
        talk, _ = self.push_to_talk(presses, backend, silence_seconds=0.1)
        listening = talk.listen()
        presses.press()

        stream = await asyncio.wait_for(anext(listening), 2)
        pcm = await said(stream, backend, [loud()] * 4 + [quiet()] * 4)

        assert len(pcm) // 2 // FRAME == 7  # it went on to the quiet
        await listening.aclose()

    async def test_it_ends_when_the_key_does(self) -> None:
        backend, presses = FakeBackend(), KeyPresses()
        talk, _ = self.push_to_talk(presses, backend)
        presses.leave()

        assert [stream async for stream in talk.listen()] == []

    async def test_a_stream_that_was_not_read_leaves_the_microphone_closed_all_the_same(
        self,
    ) -> None:
        backend, presses = FakeBackend(), KeyPresses()
        talk, _ = self.push_to_talk(presses, backend)
        listening = talk.listen()
        presses.press()
        await asyncio.wait_for(anext(listening), 2)

        await listening.aclose()

        assert backend.inputs[0].closed

    async def test_it_does_not_need_to_be_told_when_it_listens(self) -> None:
        backend, presses = FakeBackend(), KeyPresses()
        talk, _ = self.push_to_talk(presses, backend, on_listening=None)
        listening = talk.listen()
        presses.press()

        assert await asyncio.wait_for(anext(listening), 2)
        await listening.aclose()

    async def test_what_listens_for_a_stream_that_is_given_up_is_closed_with_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        closed = watching_what_listens(monkeypatch)
        backend, presses = FakeBackend(), KeyPresses()
        talk, _ = self.push_to_talk(presses, backend)
        listening = talk.listen()
        presses.press()
        stream = await asyncio.wait_for(anext(listening), 2)
        for _ in range(4):
            backend.hearing.hear(loud())
        await asyncio.wait_for(anext(stream.chunks), 2)  # it has begun, and nobody reads on

        await listening.aclose()

        assert closed == ["closed"]

    async def test_quiet_is_the_level_that_was_given(self) -> None:
        backend, presses = FakeBackend(), KeyPresses()
        talk, _ = self.push_to_talk(presses, backend, silence_level=2_000)
        listening = talk.listen()
        presses.press()
        stream = await asyncio.wait_for(anext(listening), 2)

        pcm = await said(stream, backend, [loud(1000)] * 10)

        assert len(pcm) // 2 // FRAME == 6  # 0.2 s of what is under the level ends it
        await listening.aclose()

    async def test_what_is_said_ends_at_the_longest_it_was_given(self) -> None:
        backend, presses = FakeBackend(), KeyPresses()
        talk, _ = self.push_to_talk(presses, backend, max_seconds=0.2)
        listening = talk.listen()
        presses.press()
        stream = await asyncio.wait_for(anext(listening), 2)

        pcm = await said(stream, backend, [loud()] * 20)

        assert len(pcm) // 2 // FRAME == 6  # 0.2 s of frames of 32 ms
        await listening.aclose()

    async def test_the_quiet_that_ends_it_is_the_one_that_was_given(self) -> None:
        backend, presses = FakeBackend(), KeyPresses()
        talk, _ = self.push_to_talk(presses, backend, silence_seconds=0.1)
        listening = talk.listen()
        presses.press()
        stream = await asyncio.wait_for(anext(listening), 2)

        pcm = await said(stream, backend, [loud()] + [quiet()] * 10)

        assert len(pcm) // 2 // FRAME == 4  # one that was said and 0.1 s of quiet, three frames
        await listening.aclose()


class TestAKeyOnATerminal:
    """The key as it is when somebody types: presses that queue up as the lines arrive."""

    async def test_a_press_that_came_after_what_was_said_was_over_does_not_begin_another(
        self,
    ) -> None:
        backend, typing = FakeBackend(), Typing()
        microphone = Microphone(backend, sample_rate=RATE, block_size=FRAME)
        talk = PushToTalk(
            EnterKey(cast(TextIO, typing)),
            microphone,
            silence_seconds=0.2,
            silence_level=150,
            max_seconds=5.0,
        )
        listening = talk.listen()
        try:
            typing.type()
            stream = await asyncio.wait_for(anext(listening), 2)
            await said(stream, backend, [loud()] + [quiet()] * 8)
            typing.type()  # too late: they had been quiet for long enough already
            await asyncio.sleep(0.1)  # the reader has put it in
            again = asyncio.ensure_future(anext(listening))
            await asyncio.sleep(0.1)

            assert not again.done()  # nobody asked to speak again

            typing.type()
            assert await asyncio.wait_for(again, 2)
            await listening.aclose()
        finally:
            typing.end()


class FakeEngine:
    """What watches for the word: a frame whose first sample is over 5000 has it. It remembers what
    it was asked, and on which thread it was made."""

    sample_rate = RATE
    frame_length = FRAME

    def __init__(self) -> None:
        self.processed = 0
        self.resets: list[int] = []
        self.closed = False
        self.made_on = threading.current_thread()
        self.processed_on: set[threading.Thread] = set()

    def process(self, frame: Any) -> bool:
        self.processed += 1
        self.processed_on.add(threading.current_thread())
        return int(frame[0]) > 5000

    def reset(self) -> None:
        self.resets.append(self.processed)  # how many frames it had been given when it was reset

    def close(self) -> None:
        self.closed = True


class TestAWord:
    def wake_word(
        self, backend: FakeBackend, **changes: Any
    ) -> tuple[WakeWord, list[FakeEngine], list[str]]:
        made: list[FakeEngine] = []
        told: list[str] = []

        def engine() -> FakeEngine:
            made.append(FakeEngine())
            return made[-1]

        fields: dict[str, Any] = {
            "engine": engine,
            "silence_seconds": 0.2,
            "max_seconds": 5.0,
            "on_listening": lambda: told.append("listening"),
        }
        return WakeWord(backend, **{**fields, **changes}), made, told

    async def test_what_follows_the_word_is_what_is_streamed_and_what_came_before_is_not(
        self,
    ) -> None:
        backend = FakeBackend()
        wake, made, told = self.wake_word(backend)
        listening = wake.listen()
        waiting = asyncio.ensure_future(anext(listening))
        await asyncio.sleep(0.05)

        for _ in range(5):
            backend.hearing.hear(loud(100))  # talk in the room that is not the word
        backend.hearing.hear(loud(6000))  # the word
        stream = await asyncio.wait_for(waiting, 2)
        pcm = await said(stream, backend, [loud(7), loud(8)] + [quiet()] * 8)

        [engine] = made
        assert told == ["listening"]
        assert np.frombuffer(pcm, dtype="<i2")[::FRAME].tolist()[:2] == [7, 8]
        assert engine.processed == 6
        await listening.aclose()

    async def test_the_microphone_is_opened_at_what_the_engine_takes(self) -> None:
        backend = FakeBackend()

        def engine() -> Any:  # an engine that takes what the defaults do not
            return SimpleNamespace(
                sample_rate=8_000,
                frame_length=256,
                process=lambda frame: False,
                reset=lambda: None,
                close=lambda: None,
            )

        wake, _, _ = self.wake_word(backend, device="USB", engine=engine)
        waiting = asyncio.ensure_future(anext(wake.listen()))
        await asyncio.sleep(0.05)

        stream = backend.hearing

        assert (stream.device, stream.sample_rate, stream.block_size) == ("USB", 8_000, 256)
        waiting.cancel()

    async def test_what_was_heard_while_it_was_answered_is_forgotten(self) -> None:
        backend = FakeBackend()
        wake, made, _ = self.wake_word(backend)
        listening = wake.listen()
        waiting = asyncio.ensure_future(anext(listening))
        await asyncio.sleep(0.05)
        backend.hearing.hear(loud(6000))
        stream = await asyncio.wait_for(waiting, 2)
        await said(stream, backend, [loud()] + [quiet()] * 8)
        for _ in range(10):
            backend.hearing.hear(loud(6000))  # the voice of the answer, and the word in it
        await asyncio.sleep(0.05)
        asking = asyncio.ensure_future(anext(listening))
        await asyncio.sleep(0.05)

        [engine] = made
        assert not asking.done()  # the word in the voice of the answer is not heard
        assert engine.processed == 1
        asking.cancel()

    async def test_the_engine_forgets_what_it_heard_once_the_sentence_after_the_word_is_over(
        self,
    ) -> None:
        backend = FakeBackend()
        wake, made, _ = self.wake_word(backend)
        listening, stream = await self.heard(wake, backend)
        [engine] = made
        assert engine.resets == []  # not while the sentence is being said

        await said(stream, backend, [loud()] + [quiet()] * 8)
        asking = asyncio.ensure_future(anext(listening))
        await asyncio.sleep(0.05)

        assert engine.resets == [1]  # after it, and after what was heard meanwhile was dropped
        asking.cancel()

    async def test_the_engine_is_let_go_of_when_listening_ends(self) -> None:
        backend = FakeBackend()
        wake, made, _ = self.wake_word(backend)
        listening = wake.listen()
        waiting = asyncio.ensure_future(anext(listening))
        await asyncio.sleep(0.05)
        backend.hearing.hear(loud(6000))
        await asyncio.wait_for(waiting, 2)

        await listening.aclose()

        [engine] = made
        assert engine.closed
        assert backend.inputs[0].closed

    async def test_the_engine_is_made_where_the_loop_does_not_wait_for_it(self) -> None:
        backend = FakeBackend()
        wake, made, _ = self.wake_word(backend)
        waiting = asyncio.ensure_future(anext(wake.listen()))
        await asyncio.sleep(0.05)

        [engine] = made
        assert engine.made_on is not threading.main_thread()  # it may load models, or ask a service
        waiting.cancel()

    async def test_the_frames_are_given_to_the_engine_where_the_loop_does_not_wait(self) -> None:
        backend = FakeBackend()
        wake, made, _ = self.wake_word(backend)
        waiting = asyncio.ensure_future(anext(wake.listen()))
        await asyncio.sleep(0.05)

        for _ in range(3):
            backend.hearing.hear(loud(100))
        await asyncio.sleep(0.05)

        [engine] = made
        assert engine.processed == 3
        assert engine.processed_on
        assert threading.main_thread() not in engine.processed_on
        waiting.cancel()

    async def test_an_engine_that_cannot_be_made_is_what_listening_fails_with(self) -> None:
        backend = FakeBackend()

        def engine() -> Any:
            raise WakeWordError("the models are not there")

        wake, _, _ = self.wake_word(backend, engine=engine)

        with pytest.raises(WakeWordError, match="not there"):
            await anext(wake.listen())

        assert backend.inputs == []  # a microphone was not opened for nothing

    async def test_it_does_not_need_to_be_told_when_it_listens(self) -> None:
        backend = FakeBackend()
        wake, _, _ = self.wake_word(backend, on_listening=None)
        listening = wake.listen()
        waiting = asyncio.ensure_future(anext(listening))
        await asyncio.sleep(0.05)
        backend.hearing.hear(loud(6000))

        assert await asyncio.wait_for(waiting, 2)
        await listening.aclose()

    async def heard(self, wake: WakeWord, backend: FakeBackend) -> tuple[AsyncGenerator[Any], Any]:
        """Listening, and the stream that follows the word, which a person in the room says."""
        listening = wake.listen()
        waiting = asyncio.ensure_future(anext(listening))
        await asyncio.sleep(0.05)
        backend.hearing.hear(loud(6000))
        return listening, await asyncio.wait_for(waiting, 2)

    async def test_what_listens_for_a_stream_that_is_given_up_is_closed_with_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        closed = watching_what_listens(monkeypatch)
        backend = FakeBackend()
        wake, _, _ = self.wake_word(backend)
        listening, stream = await self.heard(wake, backend)
        for _ in range(4):
            backend.hearing.hear(loud())
        await asyncio.wait_for(anext(stream.chunks), 2)  # it has begun, and nobody reads on

        await listening.aclose()

        assert closed == ["closed"]

    async def test_quiet_is_the_level_that_was_given(self) -> None:
        backend = FakeBackend()
        wake, _, _ = self.wake_word(backend, silence_level=2_000)
        listening, stream = await self.heard(wake, backend)

        pcm = await said(stream, backend, [loud(1000)] * 10)

        assert len(pcm) // 2 // FRAME == 6  # 0.2 s of what is under the level ends it
        await listening.aclose()

    async def test_what_follows_the_word_ends_at_the_longest_it_was_given(self) -> None:
        backend = FakeBackend()
        wake, _, _ = self.wake_word(backend, max_seconds=0.2)
        listening, stream = await self.heard(wake, backend)

        pcm = await said(stream, backend, [loud()] * 20)

        assert len(pcm) // 2 // FRAME == 6  # 0.2 s of frames of 32 ms
        await listening.aclose()

    async def test_the_quiet_that_ends_it_is_the_one_that_was_given(self) -> None:
        backend = FakeBackend()
        wake, _, _ = self.wake_word(backend, silence_seconds=0.1)
        listening, stream = await self.heard(wake, backend)

        pcm = await said(stream, backend, [loud()] + [quiet()] * 10)

        assert len(pcm) // 2 // FRAME == 4  # one that was said and 0.1 s of quiet, three frames
        await listening.aclose()
