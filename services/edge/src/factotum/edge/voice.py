"""The sound of an answer, made and sent while the text of it is still being written (ADR 0023).

A speaker cannot wait for the end of an answer, and it cannot wait for the sound of one sentence
before the text of the next arrives either: what a person reads or hears is the answer, and the
edge has to keep reading it from the conversation service while the voice makes what it said so
far. So the sentences that are to be said are given to a ``Voice`` as they are written, and it makes
the sound of each, in order, on a task of its own, and sends it to the device in chunks as the
voice makes it.

A device is sent a track of sound for an answer: a ``voice_start`` when the first sound is there,
a ``voice_chunk`` for each piece of it, and a ``voice_end`` once the last sentence has been said.
What the voice makes at another rate than the track began with begins another track, after the one
that ended. A sentence whose sound could not be made has none (its text was sent as a ``speech``
part all the same), and the next is tried; a voice that nothing is bound for is not asked again for
the rest of the answer.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from factotum.contracts.edge import FromEdge, VoiceChunk, VoiceEnd, VoiceStart
from factotum.contracts.errors import FactotumError, TtsNotConfiguredError
from factotum.contracts.identity import Owner
from factotum.contracts.speech import SynthesisFormat
from factotum.edge.ports import Disconnected, SpeechSource

logger = logging.getLogger("factotum.edge")

Send = Callable[[FromEdge], Awaitable[None]]


class Voice:
    """The sound of the answer to one utterance, for one device."""

    def __init__(
        self,
        *,
        speech: SpeechSource,
        owner: Owner,
        utterance_id: str,
        send: Send,
        session_id: str,
    ) -> None:
        self._speech = speech
        self._owner = owner
        self._utterance_id = utterance_id
        self._send = send
        self._session_id = session_id
        self._sentences: asyncio.Queue[tuple[int, str] | None] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._rate: int | None = None
        """The rate of the track that is open, or None when none is."""

        self._seq = 0
        self._silent = False
        """Nothing is bound to say it with: no sentence of this answer is asked for."""

        self._over = False

    def say(self, part_seq: int, text: str) -> None:
        """Have ``text`` said, once what was given before it has been: it is the ``speech`` part
        whose ``seq`` is ``part_seq``, which the device was sent before this is called."""
        if self._over or self._silent:
            return
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"voice-{self._utterance_id}")
        self._sentences.put_nowait((part_seq, text))

    async def finish(self) -> None:
        """No sentence is coming: wait until the last one has been said, and end the track."""
        if self._over:
            return
        self._over = True
        if self._task is not None:
            self._sentences.put_nowait(None)
            await asyncio.wait({self._task})

    async def abort(self) -> None:
        """Stop where it is: the sentence that is being made is given up on (and the voice that was
        making it let go of), and nothing more is sent. The turn says it was cut short."""
        self._over = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.wait({self._task})

    async def _run(self) -> None:
        try:
            while (item := await self._sentences.get()) is not None:
                part_seq, text = item
                if not self._silent:
                    await self._say(part_seq, text)
            await self._end_track()
        except Disconnected:
            return  # the device is gone: the answer finds that out for itself
        except Exception:
            logger.exception("the voice of an answer failed", extra={"session": self._session_id})

    async def _say(self, part_seq: int, text: str) -> None:
        try:
            sound = self._speech.synthesize_stream(self._owner, text)
            async with contextlib.aclosing(sound):
                async for event in sound:
                    if isinstance(event, SynthesisFormat):
                        await self._begin_track(event.sample_rate)
                    else:
                        await self._send_samples(part_seq, event.data)
        except Disconnected:
            raise
        except TtsNotConfiguredError:
            self._silent = True
        except FactotumError as error:
            logger.warning(
                "a sentence could not be spoken",
                extra={"session": self._session_id, "code": error.code},
            )
        except Exception:
            logger.exception("a sentence could not be spoken", extra={"session": self._session_id})

    async def _begin_track(self, sample_rate: int) -> None:
        """The voice is about to give samples at ``sample_rate``: open a track for them, or keep the
        one that is open when it is at that rate."""
        if self._rate == sample_rate:
            return
        await self._end_track()
        await self._send(VoiceStart(utterance_id=self._utterance_id, sample_rate=sample_rate))
        self._rate, self._seq = sample_rate, 0

    async def _send_samples(self, part_seq: int, data: bytes) -> None:
        if self._rate is None:
            return  # samples that no format came before are not a voice
        await self._send(
            VoiceChunk(utterance_id=self._utterance_id, seq=self._seq, part_seq=part_seq, data=data)
        )
        self._seq += 1

    async def _end_track(self) -> None:
        if self._rate is None:
            return
        self._rate = None
        await self._send(VoiceEnd(utterance_id=self._utterance_id))
