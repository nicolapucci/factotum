"""Staying connected: a device that loses the edge asks again, patiently.

A device on a home network is disconnected now and then (the router restarted, the edge was
updated, a Wi-Fi that dropped), and a speaker that stays silent until somebody notices is a
speaker that is broken. So what a client does with a connection is done in a loop: connect, converse
until the connection ends, wait a while, and connect again, each wait longer than the one before up
to a limit, with a little chance in it so that the devices of a home that lost the edge together do
not all ask at the same moment.

It does not go on when that would be wrong: a device the edge does not accept (its credential is
wrong, or it speaks another version of the protocol) will not be accepted by asking again, and a
device that was replaced by its own twin (another connection of the same hardware id) would take the
connection back from it, which is the two of them taking it from each other for ever.
"""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from factotum.client_core.client import DeviceClient, Refused
from factotum.client_core.connection import ConnectionLost, CouldNotConnect
from factotum.client_core.ports import Renderer
from factotum.contracts.edge import CloseCode

_ONCE = frozenset({CloseCode.REPLACED, CloseCode.POLICY, CloseCode.UNSUPPORTED_VERSION})
"""How a connection is ended by an edge that has no use for a device that comes back."""

_REFUSED_FOR_NOW = frozenset({"connection_lost"})
"""What a refusal is that is not one: the connection ended before the edge said it was ready."""


@dataclass(frozen=True, slots=True)
class Backoff:
    """How long to wait before asking again: ``first_s`` after the first failure and ``factor``
    times more after each of the ones in a row that follow, up to ``max_s``, each by up to
    ``jitter`` more or less. A connection that held for ``steady_s`` was a good one: the waits
    start over."""

    first_s: float = 1.0
    max_s: float = 30.0
    factor: float = 2.0
    jitter: float = 0.25
    steady_s: float = 30.0

    def wait_s(self, failures: int, chance: float) -> float:
        """The wait after this many failures in a row (at least one), for a ``chance`` in [0, 1)."""
        base = min(self.max_s, self.first_s * self.factor ** (max(1, failures) - 1))
        return base * (1 - self.jitter + 2 * self.jitter * chance)


class Ending(StrEnum):
    """Why a client stopped asking."""

    LEFT = "left"
    """The person left: what they were saying is over."""

    REFUSED = "refused"
    """The edge does not accept this device, and will not for asking."""

    REPLACED = "replaced"
    """Another connection of the same device took this one's place."""

    UNSUPPORTED = "unsupported"
    """The edge speaks another version of the protocol."""

    CLOSED = "closed"
    """The edge closed the connection and does not want it back."""


@dataclass(frozen=True, slots=True)
class Outcome:
    ending: Ending
    detail: str = ""

    @property
    def succeeded(self) -> bool:
        """Whether this is how it should end: the person left."""
        return self.ending is Ending.LEFT


async def stay_connected(
    open_client: Callable[[], Awaitable[DeviceClient]],
    serve: Callable[[DeviceClient], Awaitable[ConnectionLost | None]],
    renderer: Renderer,
    *,
    backoff: Backoff = Backoff(),  # noqa: B008 - a frozen value, safe to share
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    chance: Callable[[], float] = random.random,
    now: Callable[[], float] = time.monotonic,
) -> Outcome:
    """Connect, converse, and connect again when the connection is lost, until the person leaves or
    the edge makes clear that it is no use asking again.

    ``open_client`` makes a client and says hello with it (it raises ``CouldNotConnect`` when the
    edge cannot be reached and ``Refused`` when it will not have the device); ``serve`` converses
    with it until the connection is over, and returns how it ended, or None when it was the person
    who ended it. Cancelling this ends it: that is how a client is stopped.
    """
    failures = 0
    while True:
        why: str
        try:
            client = await open_client()
        except CouldNotConnect as error:
            why = str(error)
        except Refused as refused:
            if refused.code not in _REFUSED_FOR_NOW:
                return Outcome(Ending.REFUSED, str(refused))
            why = refused.message
        else:
            started = now()
            lost = await serve(client)
            if lost is None:
                return Outcome(Ending.LEFT)
            if lost.code in _ONCE:
                return Outcome(_ending_of(lost.code), lost.reason)
            if now() - started >= backoff.steady_s:
                failures = 0
            detail = f": {lost.reason}" if lost.reason else ""
            why = f"the connection ended ({lost.code}){detail}"
        failures += 1
        wait_s = backoff.wait_s(failures, chance())
        renderer.notice(f"{why}; trying again in {wait_s:.0f} s")
        await sleep(wait_s)


def _ending_of(code: int | None) -> Ending:
    if code == CloseCode.REPLACED:
        return Ending.REPLACED
    if code == CloseCode.UNSUPPORTED_VERSION:
        return Ending.UNSUPPORTED
    return Ending.CLOSED
