"""How what a device does is shown in a terminal.

The terminal cannot speak or blink a light, so it writes what a device would do, one line for
each thing, with a tag that says which part of the device is at work: ``[say]`` is the speaker,
``[show]`` and ``[card]`` the screen, ``[play]`` the media player, ``[do]`` a command that was run,
``[...]`` a hint of progress. Only ASCII, so that it reads on any console.
"""

from typing import Protocol, TextIO

from factotum.contracts.cards import Card
from factotum.contracts.edge import ErrorFrame, TurnEnded, TurnStatus
from factotum.contracts.response import StatusState

_TAG = 9
"""The width of the column of tags."""


class Renderer(Protocol):
    def speech(self, text: str) -> None: ...

    def text(self, text: str) -> None: ...

    def card(self, card: Card) -> None: ...

    def media(self, title: str | None, url: str) -> None: ...

    def status(self, state: StatusState) -> None: ...

    def event(self, what: str) -> None:
        """A command that was run on the device: the volume, a light, the music."""
        ...

    def turn_ended(self, ended: TurnEnded) -> None: ...

    def error(self, error: ErrorFrame) -> None:
        """The edge said something is wrong."""
        ...

    def violation(self, what: str) -> None:
        """The edge did something the protocol does not allow: a bug, and worth saying loudly."""
        ...

    def notice(self, what: str) -> None:
        """Something about the client itself: connected, disconnected."""
        ...


class ConsoleRenderer:
    def __init__(self, out: TextIO) -> None:
        self._out = out

    def _line(self, tag: str, text: str) -> None:
        first, *rest = text.splitlines() or [""]
        self._out.write(f"{tag:<{_TAG}}{first}\n")
        for line in rest:
            self._out.write(f"{'':<{_TAG}}{line}\n")
        self._out.flush()

    def speech(self, text: str) -> None:
        self._line("[say]", text)

    def text(self, text: str) -> None:
        self._line("[show]", text)

    def card(self, card: Card) -> None:
        lines = [card.title]
        if card.body:
            lines.append(card.body)
        lines.extend(
            f"- {item.label}" + (f": {item.value}" if item.value else "") for item in card.items
        )
        self._line("[card]", "\n".join(lines))

    def media(self, title: str | None, url: str) -> None:
        self._line("[play]", f"{title} ({url})" if title else url)

    def status(self, state: StatusState) -> None:
        self._line("[...]", state.value)

    def event(self, what: str) -> None:
        self._line("[do]", what)

    def turn_ended(self, ended: TurnEnded) -> None:
        if ended.status is TurnStatus.FAILED:
            reason = f" ({ended.code})" if ended.code else ""
            detail = f": {ended.message}" if ended.message else ""
            self._line("[!]", f"the answer failed{reason}{detail}")
        elif ended.status is TurnStatus.CANCELLED:
            self._line("[stop]", "interrupted")

    def error(self, error: ErrorFrame) -> None:
        self._line("[edge!]", f"{error.code}: {error.message}")

    def violation(self, what: str) -> None:
        self._line("[BUG]", what)

    def notice(self, what: str) -> None:
        self._line("--", what)
