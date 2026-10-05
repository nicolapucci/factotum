"""The devices of the example home, as a person would meet them: the terminal client, on the far
side of a real connection to the edge, with a profile for each (a speaker, a display, a phone).

What a person says to one is a recording (``factotum.e2e.voice``) or typed words; what it says back
is what its speaker was asked to play and what its screen was asked to show; and what a command does
to it is in ``hardware``. A recording gets to the edge the way the device carries it: whole in one
frame, or as the chunks a microphone sends as a person talks (ADR 0022), and a test is run each way.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum

from factotum.contracts.audio import AudioPayload, pcm_of
from factotum.contracts.conversation import ConversationDetail, ConversationList, Exchange
from factotum.contracts.edge import TurnEnded
from factotum.e2e.house import TIMEOUT_S, House
from factotum.e2e.voice import recording_of, said_in
from factotum.terminal.audio import AudioStream
from factotum.terminal.client import DeviceClient
from factotum.terminal.connection import WebSocketConnection
from factotum.terminal.device import TerminalDevice
from factotum.terminal.profiles import get_profile
from factotum.terminal.testing import FakeVoiceInput, FakeVoiceOutput, RecordingRenderer


class Carrier(StrEnum):
    """How a recording gets to the edge."""

    CLIP = "clip"
    """Whole, in one frame, after the person has stopped."""

    STREAM = "stream"
    """In chunks, from when the person begins to when they stop."""


DEFAULT_CARRIER = Carrier.CLIP
"""How a device hands a recording over unless a test says: the tests of ``e2e/tests`` run each way
(see its ``conftest.py``), which is what changes this."""


@dataclass
class Device:
    """A device in a terminal."""

    client: DeviceClient
    shown: RecordingRenderer
    speaker: FakeVoiceOutput
    hardware: TerminalDevice
    """What the commands it was given did to it: its volume, its light, its screen."""

    timeout_s: float = TIMEOUT_S
    """How long it waits for an answer to end."""

    carrier: Carrier = Carrier.CLIP
    """How it hands a recording over."""

    def spoken(self) -> list[str]:
        """What the speaker was made to say, read back out of the clips it was asked to play (the
        clips of the stand-in of Piper: the words are in them)."""
        return [said_in(clip) for clip in self.speaker.played]

    def heard_aloud(self, listener: Callable[[AudioPayload], str]) -> list[str]:
        """What the speaker was made to say, as somebody who listens to it hears it: a real voice
        carries its words in sound, and the listener (a Whisper) is who reads them back."""
        return [listener(clip) for clip in self.speaker.played]

    def understood(self) -> list[str]:
        """What the edge said it took each recording for, before it answered: what the assistant
        made of what was said, as the device is told it."""
        return list(self.shown.of("heard"))

    async def says_aloud(self, words: str) -> TurnEnded:
        """Says something with a recording of it, and waits for the answer to end."""
        return await self.sends(recording_of(words))

    async def sends(self, recording: AudioPayload) -> TurnEnded:
        """Hands a recording over, the way this device does, and waits for the answer to end."""
        return await self.answered(await self.hands_over(recording))

    async def hands_over(self, recording: AudioPayload) -> str:
        """Hands a recording over, whole or as the chunks a microphone would send, and returns what
        it is called, without waiting for the answer."""
        if self.carrier is Carrier.STREAM:
            pcm, sample_rate = pcm_of(recording.data)
            return await self.client.say_audio_stream(AudioStream.of(pcm, sample_rate))
        return await self.client.say_audio(recording)

    def microphone(self, *words: str) -> FakeVoiceInput:
        """What a microphone hands the terminal for each wake word that was heard, when somebody
        says each of these: the recordings, or the streams, according to how this device carries
        them."""
        recordings = [recording_of(said) for said in words]
        if self.carrier is Carrier.CLIP:
            return FakeVoiceInput(*recordings)
        return FakeVoiceInput(
            *(AudioStream.of(*pcm_of(recording.data)) for recording in recordings)
        )

    async def types(self, words: str) -> TurnEnded:
        return await self.answered(await self.client.say(words))

    async def answered(self, identity: str) -> TurnEnded:
        """Waits for the answer to what was said to end, and says how it did."""
        ended = await asyncio.wait_for(self.client.wait_for_turn(identity), self.timeout_s)
        assert ended is not None, "the connection ended before the answer did"
        return ended


@asynccontextmanager
async def joined(
    house: House, name: str, *, credential: str | None = None
) -> AsyncIterator[Device]:
    """The device of this profile (``speaker``, ``smart-display``, ``phone``...), connected to the
    edge and told it is ready. It does not yet do what the edge sends: whoever takes it on does
    (``client.run()``). It presents the credential it was given when it was paired (the hub's) or
    the one of its profile, unless ``credential`` says another."""
    profile = get_profile(name)
    shown, speaker = RecordingRenderer(), FakeVoiceOutput()
    hardware = TerminalDevice(profile.capabilities, shown)
    client = DeviceClient(
        await WebSocketConnection.open(house.edge_url),
        profile,
        shown,
        hardware,
        token=credential or house.tokens.get(profile.hardware_id),
        voice_output=speaker,
    )
    try:
        await client.connect()
        yield Device(client, shown, speaker, hardware, house.timeout_s, DEFAULT_CARRIER)
    finally:
        await client.close()


@asynccontextmanager
async def connected(
    house: House, name: str, *, credential: str | None = None
) -> AsyncIterator[Device]:
    """The same, doing what the edge sends."""
    async with joined(house, name, credential=credential) as device:
        listening = asyncio.create_task(device.client.run())
        try:
            yield device
        finally:
            await device.client.close()
            await asyncio.wait({listening})


async def until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(TIMEOUT_S):
        while not condition():  # noqa: ASYNC110 - a state to watch, there is no event
            await asyncio.sleep(0.005)


async def remembered(house: House) -> list[Exchange]:
    """What the conversation service kept of what a person said, read as that person."""
    me = house.admin
    listed = await house.conversation.get("/v1/conversations", headers=me)
    exchanges: list[Exchange] = []
    for conversation in ConversationList.model_validate(listed.json()).conversations:
        detail = await house.conversation.get(f"/v1/conversations/{conversation.id}", headers=me)
        exchanges.extend(ConversationDetail.model_validate(detail.json()).exchanges)
    return exchanges
