"""What every Factotum client shares (ADR 0024): the protocol with the edge, what a person says
sent as they say it, the sound of an answer played as it arrives, and the ports that the hardware of
each device plugs into.

A client is made by giving ``DeviceClient`` the ports its device has, and nothing else: a speaker
has an identity, a renderer, hands for its commands and a voice sink; a screen has a renderer that
draws; a test has fakes (``factotum.client_core.testing``). What it declares to the edge is what it
was given.
"""

from factotum.client_core.audio import (
    DEFAULT_CHUNK_MS,
    AudioStream,
    VoiceInput,
    VoiceOutput,
    VoiceSink,
)
from factotum.client_core.client import CLIENT, CannotConverse, DeviceClient, Refused
from factotum.client_core.connection import (
    Connection,
    ConnectionLost,
    CouldNotConnect,
    WebSocketConnection,
)
from factotum.client_core.loop import VoiceLoop
from factotum.client_core.moments import next_moment
from factotum.client_core.playback import VoicePlayer
from factotum.client_core.ports import (
    CommandDriver,
    CommandHandler,
    CommandRouter,
    DeviceIdentity,
    Identity,
    Renderer,
)
from factotum.client_core.reconnect import Backoff, Ending, Outcome, stay_connected
from factotum.client_core.timekeeping import Clock, Ring, SystemClock, Timekeeper

__all__ = [
    "CLIENT",
    "DEFAULT_CHUNK_MS",
    "AudioStream",
    "Backoff",
    "CannotConverse",
    "Clock",
    "CommandDriver",
    "CommandHandler",
    "CommandRouter",
    "Connection",
    "ConnectionLost",
    "CouldNotConnect",
    "DeviceClient",
    "DeviceIdentity",
    "Ending",
    "Identity",
    "Outcome",
    "Refused",
    "Renderer",
    "Ring",
    "SystemClock",
    "Timekeeper",
    "VoiceInput",
    "VoiceLoop",
    "VoiceOutput",
    "VoicePlayer",
    "VoiceSink",
    "WebSocketConnection",
    "next_moment",
    "stay_connected",
]
