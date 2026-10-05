"""Stand-ins for what the terminal depends on, so that its tests need no keyboard: a person who
types. The rest of what a client is tested with (a connection in memory, a renderer that writes
down what it was shown, speakers) is the core's: ``factotum.client_core.testing``."""

import asyncio
import inspect
from collections.abc import AsyncIterator, Callable
from typing import Any


class Typed:
    """What a person types, a line at a time. Where a line is a function it is run when it is
    reached (and awaited, if it is asynchronous), which is how the edge is made to say something in
    between."""

    def __init__(self, *lines: str | Callable[[], Any]) -> None:
        self._lines = lines

    async def read(self) -> AsyncIterator[str | None]:
        for line in self._lines:
            if isinstance(line, str):
                yield line
            elif inspect.isawaitable(waiting := line()):
                await waiting
            # The terminal is given the time to do what it has been told, as it would be by the
            # seconds a person takes to type.
            await asyncio.sleep(0.01)
        yield None
