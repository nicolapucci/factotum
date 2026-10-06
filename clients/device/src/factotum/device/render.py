"""What a device says of itself, in lines that a person can read in a terminal or a journal.

A speaker has no screen, so what it shows is what is happening: what it heard, what it is saying,
what went wrong. One line for each thing, with a tag that says which it is, in ASCII only so that it
reads on any console (and in ``journalctl``). What a person sees is a log of the conversation, and
nothing in it is a secret: a credential is never in what the edge sends, and never in a line here.
"""

from typing import TextIO

from factotum.contracts.cards import Card
from factotum.contracts.edge import ErrorFrame, TurnEnded, TurnStatus
from factotum.contracts.response import StatusState

_TAG = 9


class DeviceRenderer:
    """A ``factotum.client_core.Renderer`` that writes lines. ``quiet`` leaves out the words of the
    answer (what is said is heard) and keeps what went wrong."""

    def __init__(self, out: TextIO, *, quiet: bool = False) -> None:
        self._out = out
        self._quiet = quiet

    def _line(self, tag: str, text: str) -> None:
        first, *rest = text.splitlines() or [""]
        self._out.write(f"{tag:<{_TAG}}{first}\n")
        for line in rest:
            self._out.write(f"{'':<{_TAG}}{line}\n")
        self._out.flush()

    def speech(self, text: str) -> None:
        if not self._quiet:
            self._line("[say]", text)

    def text(self, text: str) -> None:
        """A speaker shows no text: the edge does not send it one."""

    def card(self, card: Card) -> None:
        """Nor a card."""

    def media(self, title: str | None, url: str) -> None:
        """Nor does it play music of its own."""

    def status(self, state: StatusState) -> None:
        if not self._quiet:
            self._line("[...]", state.value)

    def event(self, what: str) -> None:
        self._line("[do]", what)

    def heard(self, text: str) -> None:
        self._line("[heard]", text)

    def turn_ended(self, ended: TurnEnded) -> None:
        if ended.status is TurnStatus.FAILED:
            reason = f" ({ended.code})" if ended.code else ""
            detail = f": {ended.message}" if ended.message else ""
            self._line("[!]", f"the answer failed{reason}{detail}")
        elif ended.status is TurnStatus.CANCELLED and not self._quiet:
            self._line("[stop]", "interrupted")

    def error(self, error: ErrorFrame) -> None:
        self._line("[edge!]", f"{error.code}: {error.message}")

    def violation(self, what: str) -> None:
        self._line("[BUG]", what)

    def notice(self, what: str) -> None:
        self._line("--", what)
