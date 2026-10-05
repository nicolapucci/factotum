"""A device seen from the wire: the frames of the protocol as they are sent and as they arrive, with
no client library between a test and the edge.

The terminal client (``factotum.e2e.devices``) is how a person meets the assistant, and what a test
of it looks at is what a device did. This is for what the *edge* sent and in which order: the
frames of a voice that is streamed out (ADR 0023), whole, as they went over a real socket, and a
device that asks for them or does not. It is not a client: it does nothing with what it is sent,
and a test reads what came.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.client import ClientConnection, connect

from factotum.contracts.audio import AudioPayload, pcm_of, slices, wav_of
from factotum.contracts.edge import (
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    AudioChunk,
    AudioEnd,
    AudioStart,
    AudioUtterance,
    Cancel,
    Feature,
    Hello,
    Ready,
    TurnEnded,
    Utterance,
    decode_edge_frame,
    encode_frame,
)
from factotum.contracts.response import Speech
from factotum.e2e import devices
from factotum.e2e.devices import Carrier
from factotum.e2e.house import House
from factotum.e2e.voice import recording_of, words_in
from factotum.terminal.profiles import get_profile

CHUNK_BYTES = 3200
"""A tenth of a second of what a microphone hands over (16 kHz): what the probe sends at a time."""


@dataclass(frozen=True)
class Track:
    """The sound that one ``voice_start`` opened, and one ``voice_end`` or the end of the turn
    closed."""

    sample_rate: int
    by_part: dict[int, bytes]
    """What was said of each ``speech`` part of the answer, by its ``seq``, in samples."""

    ended: bool
    """Whether a ``voice_end`` closed it: a track that a turn cut short has none."""

    def said(self, part_seq: int) -> str:
        """The words that the samples of a part carry, read back (the voice of the stand-in of
        Piper writes its words into its samples)."""
        return words_in(wav_of(self.by_part[part_seq], self.sample_rate).data)


@dataclass(frozen=True)
class Turn:
    """What the edge sent for one utterance, in the order it sent it."""

    utterance_id: str
    frames: list[Any]

    def of(self, *kinds: str) -> list[Any]:
        return [frame for frame in self.frames if frame.type in kinds]

    @property
    def kinds(self) -> list[str]:
        return [frame.type for frame in self.frames]

    @property
    def ended(self) -> TurnEnded:
        [ended] = self.of("turn_ended")
        assert isinstance(ended, TurnEnded)
        return ended

    def spoken(self) -> list[tuple[int, Speech]]:
        """The ``speech`` parts, each with its ``seq``."""
        return [(f.seq, f.part) for f in self.of("part") if isinstance(f.part, Speech)]

    def sound(self) -> list[Track]:
        """The tracks of sound of the answer, in order, as they were sent."""
        tracks: list[Track] = []
        open_rate: int | None = None
        by_part: dict[int, bytearray] = {}
        for frame in self.frames:
            if frame.type == "voice_start":
                open_rate, by_part = frame.sample_rate, {}
            elif frame.type == "voice_chunk":
                by_part.setdefault(frame.part_seq, bytearray()).extend(frame.data)
            elif frame.type == "voice_end" and open_rate is not None:
                tracks.append(Track(open_rate, {k: bytes(v) for k, v in by_part.items()}, True))
                open_rate = None
        if open_rate is not None:
            tracks.append(Track(open_rate, {k: bytes(v) for k, v in by_part.items()}, False))
        return tracks


class Probe:
    """A connection to the edge that says hello and then only sends what a test says and keeps
    everything it is sent."""

    def __init__(self, connection: ClientConnection, ready: Ready, timeout_s: float) -> None:
        self._connection = connection
        self.ready = ready
        self.frames: list[Any] = []
        self._timeout_s = timeout_s
        self._changed = asyncio.Event()
        self._count = 0
        self._reading = asyncio.create_task(self._read())

    async def _read(self) -> None:
        with suppress(Exception):  # a connection that is over is what ends the reading
            async for message in self._connection:
                self.frames.append(decode_edge_frame(message))
                self._changed.set()

    async def close(self) -> None:
        self._reading.cancel()
        await asyncio.wait({self._reading})
        await self._connection.close()

    # ---- what a person says ----

    def _identity(self) -> str:
        self._count += 1
        return f"probe-{self._count}"

    async def types(self, words: str) -> str:
        identity = self._identity()
        await self._connection.send(encode_frame(Utterance(id=identity, text=words)))
        return identity

    async def says(self, words: str) -> str:
        """A recording of ``words``, carried the way the carrier of the test says (``Carrier``)."""
        recording = recording_of(words)
        if devices.DEFAULT_CARRIER is Carrier.STREAM:
            return await self.begins(recording)
        identity = self._identity()
        await self._connection.send(encode_frame(AudioUtterance(id=identity, audio=recording)))
        return identity

    async def begins(self, recording: AudioPayload, *, finish: bool = True) -> str:
        """Begins to say a recording as a stream, and ends it unless ``finish`` is false."""
        identity = self._identity()
        pcm, rate = pcm_of(recording.data)
        await self._connection.send(encode_frame(AudioStart(id=identity, sample_rate=rate)))
        for seq, piece in enumerate(slices(pcm, CHUNK_BYTES)):
            await self._connection.send(encode_frame(AudioChunk(id=identity, seq=seq, data=piece)))
        if finish:
            await self._connection.send(encode_frame(AudioEnd(id=identity)))
        return identity

    async def ends(self, identity: str) -> None:
        """The person stops talking: ends a stream that ``begins`` left open."""
        await self._connection.send(encode_frame(AudioEnd(id=identity)))

    async def cancels(self, identity: str) -> None:
        await self._connection.send(encode_frame(Cancel(id=identity)))

    # ---- what the edge says ----

    async def until(self, wanted: str, identity: str | None = None) -> None:
        """Waits until a frame of this kind (of this utterance) has arrived."""
        async with asyncio.timeout(self._timeout_s):
            while not self.has(wanted, identity):
                self._changed.clear()
                await self._changed.wait()

    def has(self, kind: str, identity: str | None = None) -> bool:
        return any(
            frame.type == kind
            and (identity is None or getattr(frame, "utterance_id", None) == identity)
            for frame in self.frames
        )

    async def turn(self, identity: str) -> Turn:
        """Everything the edge sent for an utterance, once its turn has ended."""
        await self.until("turn_ended", identity)
        return self.so_far(identity)

    def so_far(self, identity: str) -> Turn:
        return Turn(
            identity,
            [f for f in self.frames if getattr(f, "utterance_id", None) == identity],
        )


@asynccontextmanager
async def probe(
    house: House, name: str = "speaker", *, features: tuple[Feature, ...] = (Feature.VOICE_STREAM,)
) -> AsyncIterator[Probe]:
    """A device of this profile on the wire, that says hello (taking what voice it is asked for) and
    is told it is ready."""
    profile = get_profile(name)
    hello = Hello(
        protocol_version=PROTOCOL_VERSION,
        hardware_id=profile.hardware_id,
        token=house.tokens.get(profile.hardware_id) or profile.token,
        capabilities=frozenset(profile.capabilities),
        features=frozenset(features),
    )
    async with connect(house.edge_url, max_size=MAX_FRAME_BYTES) as connection:
        await connection.send(encode_frame(hello))
        ready = decode_edge_frame(await asyncio.wait_for(connection.recv(), house.timeout_s))
        assert isinstance(ready, Ready), ready
        device = Probe(connection, ready, house.timeout_s)
        try:
            yield device
        finally:
            await device.close()
