"""Conversing by voice: what a person says is sent as they say it, and the answer is given.

``VoiceLoop`` is what a client with a microphone does with it, whatever the microphone is (a real
one watched by a wake word, a key that is pushed to talk, a file): wait for a person to begin,
send what they say as they say it (ADR 0022), and either wait for the answer to be given and played
before listening again, or go on listening while it plays, so that a person who speaks over it stops
it (the answer is theirs to cut, and the client stops what plays before the edge is told).

Which of the two is the device's: a speaker that hears its own voice in its microphone has to wait,
and one with headphones, or that cancels its own echo, does not.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from factotum.client_core.audio import AudioStream, VoiceInput
from factotum.client_core.client import DeviceClient
from factotum.client_core.connection import ConnectionLost
from factotum.contracts.audio import AudioPayload

_Said = AudioPayload | AudioStream


class VoiceLoop:
    """The conversation of a client with a microphone, which outlives the connections it has.

    The microphone is opened once, the first time something is waited for from it, and kept: a
    device that loses the edge and finds it again does not open its microphone again (a wake-word
    engine takes seconds, and checks a key over the network). What a person says while the device
    is not connected has nobody to be said to, and is let go of.
    """

    def __init__(self, voice_input: VoiceInput, *, listen_while_answering: bool = False) -> None:
        self._voice_input = voice_input
        self._listen_while_answering = listen_while_answering
        self._heard: AsyncIterator[_Said] | None = None
        self._pending: asyncio.Future[_Said | None] | None = None

    async def serve(self, client: DeviceClient) -> ConnectionLost | None:
        """Converse until the person has gone or the connection is, and say goodbye. Returns how the
        connection ended, if it was not the person who ended it. What is said is over when the
        microphone has no more (the files of ``--wav-in`` end): the person has left."""
        listening = asyncio.create_task(client.run())
        lost = False
        try:
            while True:
                said = await self._next(listening)
                if said is None:
                    break
                try:
                    if isinstance(said, AudioStream):
                        identity = await client.say_audio_stream(said)
                    else:
                        identity = await client.say_audio(said)
                except ConnectionLost:
                    lost = True
                    break
                if not self._listen_while_answering:
                    await client.wait_for_turn(identity)
                    await client.wait_until_played()
            if not lost and client.active is not None:
                # What was said is over, and the answer to the last of it is still due.
                await client.wait_for_turn(client.active)
            if not lost:
                await client.wait_until_played()
        finally:
            lost = lost or listening.done()
            await client.close()
            await asyncio.wait({listening})
        return listening.result() if lost else None

    async def _next(self, listening: "asyncio.Task[ConnectionLost | None]") -> _Said | None:
        """What a person says next, or None when there is no more of it or the connection is gone
        (which is not to wait for them to speak in order to be noticed)."""
        if self._heard is None:
            self._heard = aiter(self._voice_input.listen())
        if self._pending is None:
            self._pending = asyncio.ensure_future(anext(self._heard, None))
        waiting: set[asyncio.Future[Any]] = {self._pending, listening}
        await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
        if listening.done():
            # The connection is gone, and what may have been heard in the same moment has nobody to
            # be said to: it would be sent to nothing, and its answer waited for. What has not been
            # heard yet is waited for again, by the next connection.
            if self._pending.done():
                pending, self._pending = self._pending, None
                said = pending.result()
                if isinstance(said, AudioStream):
                    await said.chunks.aclose()
            return None
        pending, self._pending = self._pending, None
        return pending.result()
