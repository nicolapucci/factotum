"""The devices of the example home, as a person would meet them: the client of the core
(``factotum.client_core``) with the hardware of the terminal, on the far side of a real connection
to the edge, with a profile for each (a speaker, a display, a phone).

What a person says to one is a recording (``factotum.e2e.voice``) or typed words; what it says back
is what its speaker was asked to play and what its screen was asked to show; and what a command does
to it is in ``hardware``. The way a device carries sound is how a test is run, each way: a recording
gets to the edge whole in one frame and the voice of the answer comes back as a clip in each
sentence, or what a person says is sent as the chunks a microphone sends as they say it (ADR 0022)
and the voice of the answer is taken as a stream (ADR 0023), which a speaker is given as it
arrives.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum

from factotum.client_core import AudioStream, DeviceClient, WebSocketConnection
from factotum.client_core.testing import (
    FakeSpeaker,
    FakeVoiceInput,
    FakeVoiceOutput,
    RecordingRenderer,
)
from factotum.contracts.audio import AudioPayload, pcm_of
from factotum.contracts.conversation import ConversationDetail, ConversationList, Exchange
from factotum.contracts.edge import TurnEnded
from factotum.e2e.house import TIMEOUT_S, House
from factotum.e2e.voice import recording_of, said_in
from factotum.terminal.device import TerminalDevice
from factotum.terminal.profiles import get_profile


class Carrier(StrEnum):
    """How sound gets to the edge and back from it."""

    CLIP = "clip"
    """A recording is sent whole, in one frame, after the person has stopped; the voice of the
    answer is a clip in each sentence, which a speaker plays whole."""

    STREAM = "stream"
    """What a person says is sent in chunks, from when they begin to when they stop; the voice of
    the answer is taken as a stream, which a speaker is given as it arrives."""


DEFAULT_CARRIER = Carrier.CLIP
"""How a device carries sound unless a test says: the tests of ``e2e/tests`` run each way (see its
``conftest.py``), which is what changes this."""


@dataclass
class Device:
    """A device in a terminal."""

    client: DeviceClient
    shown: RecordingRenderer
    speaker: FakeVoiceOutput | FakeSpeaker
    """What plays: clips whole, or (when the carrier is the stream) what it is given as it
    arrives."""

    hardware: TerminalDevice
    """What the commands it was given did to it: its volume, its light, its screen."""

    timeout_s: float = TIMEOUT_S
    """How long it waits for an answer to end."""

    carrier: Carrier = Carrier.CLIP
    """How it hands a recording over."""

    def sentences(self) -> list[AudioPayload]:
        """What the speaker was made to say, a clip for each sentence: the clips that it was asked
        to play, or the sound that it was given, sentence by sentence."""
        if isinstance(self.speaker, FakeSpeaker):
            return self.speaker.sentences()
        return list(self.speaker.played)

    def spoken(self) -> list[str]:
        """What the speaker was made to say, read back out of its sentences (the clips of the
        stand-in of Piper: the words are in them)."""
        return [said_in(clip) for clip in self.sentences()]

    def heard_aloud(self, listener: Callable[[AudioPayload], str]) -> list[str]:
        """What the speaker was made to say, as somebody who listens to it hears it: a real voice
        carries its words in sound, and the listener (a Whisper) is who reads them back."""
        return [listener(clip) for clip in self.sentences()]

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
        """Waits for the answer to what was said to end, and for what was received of its sound to
        be played (what a skill had it say besides the answer, as well), and says how the answer
        ended."""
        ended = await asyncio.wait_for(self.client.wait_for_turn(identity), self.timeout_s)
        assert ended is not None, "the connection ended before the answer did"
        await asyncio.wait_for(self.client.wait_until_played(), self.timeout_s)
        return ended


@asynccontextmanager
async def joined(
    house: House,
    name: str,
    *,
    credential: str | None = None,
    plays_clips: bool = True,
) -> AsyncIterator[Device]:
    """The device of this profile (``speaker``, ``smart-display``, ``phone``...), connected to the
    edge and told it is ready. It does not yet do what the edge sends: whoever takes it on does
    (``client.run()``). It presents the credential it was given when it was paired (the hub's) or
    the one of its profile, unless ``credential`` says another. ``plays_clips=False`` is a device
    that has no sound port for the client to give a voice to: it declares no feature, and is sent
    the words of what it is to say, as devices were before there were clips."""
    profile = get_profile(name)
    carrier = DEFAULT_CARRIER
    shown = RecordingRenderer()
    speaker: FakeVoiceOutput | FakeSpeaker
    speaker = FakeSpeaker() if carrier is Carrier.STREAM else FakeVoiceOutput()
    hardware = TerminalDevice(profile.capabilities, shown)
    client = DeviceClient(
        await WebSocketConnection.open(house.edge_url),
        profile,
        shown,
        hardware,
        token=credential or house.tokens.get(profile.hardware_id),
        voice_output=None if carrier is Carrier.STREAM or not plays_clips else speaker,
        voice_sink=speaker if isinstance(speaker, FakeSpeaker) and plays_clips else None,
    )
    try:
        await client.connect()
        yield Device(client, shown, speaker, hardware, house.timeout_s, carrier)
    finally:
        await client.close()


@asynccontextmanager
async def connected(
    house: House,
    name: str,
    *,
    credential: str | None = None,
    plays_clips: bool = True,
) -> AsyncIterator[Device]:
    """The same, doing what the edge sends."""
    async with joined(house, name, credential=credential, plays_clips=plays_clips) as device:
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
