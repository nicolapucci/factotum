"""The key that a person presses to speak: Enter in a terminal, and what ends it."""

import asyncio
import io
import threading
import time
from typing import TextIO, cast

import pytest

from factotum.client_audio.keys import EnterKey


async def presses_of(key: EnterKey) -> int:
    return len([press async for press in key])


class TestPressingEnter:
    async def test_each_line_is_a_press_and_the_end_of_the_input_is_the_end(self) -> None:
        key = EnterKey(io.StringIO("\n\nwhatever is typed\n"))

        assert await asyncio.wait_for(presses_of(key), 2) == 3

    @pytest.mark.parametrize("leaving", ["q", "Q", "quit", "exit", "  q  "])
    async def test_a_person_who_types_q_has_gone(self, leaving: str) -> None:
        key = EnterKey(io.StringIO(f"\n{leaving}\n\n\n"))

        assert await asyncio.wait_for(presses_of(key), 2) == 1  # what came after is not heard

    async def test_it_ends_once_and_stays_ended(self) -> None:
        key = EnterKey(io.StringIO(""))

        assert await asyncio.wait_for(presses_of(key), 2) == 0
        assert await presses_of(key) == 0

    async def test_a_press_that_comes_while_it_is_answered_is_asked_about_once(self) -> None:
        key = EnterKey(io.StringIO("\n\n\n"))
        iterator = aiter(key)
        await asyncio.wait_for(anext(iterator), 2)  # the first press begins
        await asyncio.sleep(0.05)  # the reader has put the others in

        assert key.pending() is True  # they said they have done
        assert key.pending() is False  # and that was taken

    async def test_nothing_pressed_is_nothing_pending(self) -> None:
        key = EnterKey(io.StringIO(""))

        assert key.pending() is False  # not started
        assert await asyncio.wait_for(presses_of(key), 2) == 0
        assert key.pending() is False

    async def test_the_end_of_the_input_is_not_taken_for_a_press_and_is_not_lost(self) -> None:
        key = EnterKey(io.StringIO("\n"))
        iterator = aiter(key)
        await asyncio.wait_for(anext(iterator), 2)
        await asyncio.sleep(0.05)  # the end of the input has been put in

        assert key.pending() is False

        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(iterator), 2)  # it is still the end

    async def test_lines_are_read_when_the_first_press_is_waited_for_and_not_before(self) -> None:
        stream = io.StringIO("\n")
        key = EnterKey(stream)
        aiter(key)

        assert stream.tell() == 0

        await asyncio.wait_for(anext(key), 2)

        assert stream.tell() == 1

    def test_a_reader_whose_loop_is_gone_does_not_fail(self) -> None:
        # The reading is on a thread of its own and ends when the input does; what it says to a
        # loop that has gone is nothing (a thread that failed would fail the test, as every warning
        # is an error).
        class Slow:
            def __init__(self, text: str) -> None:
                self._lines = io.StringIO(text)

            def readline(self) -> str:
                time.sleep(0.1)
                return self._lines.readline()

        async def start_and_leave() -> None:
            EnterKey(cast(TextIO, Slow("\n")))._started()

        asyncio.run(start_and_leave())

        for thread in threading.enumerate():
            if thread.name == "enter-key":
                thread.join(2)
