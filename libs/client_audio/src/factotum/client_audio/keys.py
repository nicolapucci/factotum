"""A key that a person presses to speak.

A terminal tells a program what was typed a line at a time, and nothing of a key held down or let
go, so the key is Enter: the first press begins, the next is the person saying they have done (what
ends it anyway is their being quiet). ``q`` and Enter, or the end of the input, say that the
person has gone.
"""

import asyncio
import contextlib
import sys
import threading
from typing import TextIO

LEAVING = frozenset({"q", "quit", "exit"})
"""What a person types to say they are done with the assistant, and not with a sentence."""


class EnterKey:
    """The presses of Enter in a terminal, as an iterator: each is the person asking to speak. A
    line that says ``q``, or the end of the input, ends it. The lines are read on a thread of
    their own (a read of a terminal cannot be cancelled), which is a daemon: it does not keep the
    program from ending."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream
        self._queue: asyncio.Queue[bool] | None = None
        self._ended = False

    def _started(self) -> "asyncio.Queue[bool]":
        if self._queue is None:
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue[bool] = asyncio.Queue()
            stream = self._stream or sys.stdin

            def read() -> None:
                def put(pressed: bool) -> None:
                    with contextlib.suppress(RuntimeError):  # the loop may be gone
                        loop.call_soon_threadsafe(queue.put_nowait, pressed)

                for line in iter(stream.readline, ""):
                    if line.strip().casefold() in LEAVING:
                        break
                    put(True)
                put(False)

            threading.Thread(target=read, name="enter-key", daemon=True).start()
            self._queue = queue
        return self._queue

    def __aiter__(self) -> "EnterKey":
        return self

    async def __anext__(self) -> None:
        if self._ended:
            raise StopAsyncIteration
        if await self._started().get():
            return None
        self._ended = True
        raise StopAsyncIteration

    def pending(self) -> bool:
        """Whether Enter was pressed since the last time this was asked, or since the press that is
        being answered: a person who presses it again is saying they have done. A press is taken
        by being asked about once."""
        if self._queue is None:
            return False
        pressed = False
        while not self._queue.empty():
            if self._queue.get_nowait():
                pressed = True
            else:  # the end of the input stays the end of it
                self._queue.put_nowait(False)
                break
        return pressed
