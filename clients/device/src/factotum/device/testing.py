"""What a test of the device application is made of: settings that a test writes in a line, and an
edge that a test plays over in-memory connections, a new one each time the device asks for one.

The sound card is ``factotum.client_audio.testing.FakeBackend``, and the rest (the connection, the
renderer) are the core's (``factotum.client_core.testing``): what is run is the application, with
nothing replaced but the hardware and the network.
"""

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from factotum.client_core import Connection, CouldNotConnect
from factotum.client_core.testing import MemoryConnection
from factotum.contracts.capabilities import Capability
from factotum.contracts.edge import PROTOCOL_VERSION, Feature, Ready
from factotum.contracts.ids import DeviceId
from factotum.device.config import Config


def a_config(**sections: dict[str, Any]) -> Config:
    """The settings of the kitchen speaker of the example home, with what a test changes: a section
    is given as a mapping, and what it says is put over what the default of the section says."""
    given: dict[str, Any] = {
        "device": {"hardware_id": "kitchen-01", "token": "kitchen-credential"},
        "reconnect": {"first_s": 0.01, "max_s": 0.02},
    }
    for name, changes in sections.items():
        given[name] = {**given.get(name, {}), **changes}
    return Config.model_validate(given)


def ready(
    capabilities: frozenset[Capability] = frozenset({Capability.AUDIO_IN, Capability.AUDIO_OUT}),
    *,
    features: frozenset[Feature] = frozenset({Feature.VOICE_STREAM}),
    **changes: Any,
) -> Ready:
    """What the edge says when it knows the device and will stream it the voice of an answer."""
    fields: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "session_id": "s1",
        "device_id": DeviceId(uuid4()),
        "name": "Kitchen speaker",
        "capabilities": capabilities,
        "features": features,
    }
    return Ready(**{**fields, **changes})


class Edge:
    """The edge, as a device meets it: each time the device asks for a connection it is given the
    next of those that the test made, and when there are none left the edge cannot be reached."""

    def __init__(self, *connections: MemoryConnection, failing: int = 0) -> None:
        self.connections = list(connections) or [MemoryConnection()]
        self.failing = failing
        """How many of the first times that the device asks, the edge cannot be reached."""

        self.asked: list[str] = []
        """The addresses that the device asked for, in order."""

    @property
    def connection(self) -> MemoryConnection:
        """The first connection, for a test that has one."""
        return self.connections[0]

    async def open(self, url: str) -> Connection:
        self.asked.append(url)
        reachable = len(self.asked) - self.failing
        if reachable < 1 or reachable > len(self.connections):
            raise CouldNotConnect("connection refused")
        return self.connections[reachable - 1]


async def until(condition: Callable[[], object], seconds: float = 2.0) -> None:
    """Wait until something that the device does on its own has been done."""
    async with asyncio.timeout(seconds):
        while not condition():  # noqa: ASYNC110 - a state to watch, there is no event
            await asyncio.sleep(0.005)
