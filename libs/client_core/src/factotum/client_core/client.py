"""The device's side of the protocol: the one implementation that every client shares.

It says hello and is told whether it is known; keeps in touch, since a device that says nothing
for a minute is taken to be gone; sends what a person said and waits for the answer to end; and
does what the edge sends: it renders the parts of an answer, plays the sound of it, and runs the
commands it is given and says what became of each.

The sound of an answer comes in the way the device said it takes (ADR 0023): a clip in each
``speech`` part, which a ``VoiceOutput`` plays, or, to a device with a ``VoiceSink``, a track of
chunks as the voice makes them, which a ``VoicePlayer`` gives the sink without holding up the frames
that follow. A person who speaks while an answer plays stops it on the spot, before the edge is told
(ADR 0022): what plays is the device's.

It also checks the edge. A part that this device could not render, parts out of order, an
answer that nobody asked for, sound after the end of its turn: the edge is not to do any of it, and
a device that saw it would say so. That is what a client is good for, before there is a real one.
"""

import asyncio
import contextlib
from dataclasses import dataclass

from factotum.client_core.audio import AudioStream, VoiceOutput, VoiceSink
from factotum.client_core.connection import Connection, ConnectionLost
from factotum.client_core.playback import VoicePlayer
from factotum.client_core.ports import CommandHandler, Identity, Renderer
from factotum.contracts.audio import AudioPayload
from factotum.contracts.capabilities import Capability
from factotum.contracts.commands import CommandStatus, PlayUrl
from factotum.contracts.edge import (
    PROTOCOL_VERSION,
    Ack,
    AudioChunk,
    AudioEnd,
    AudioStart,
    AudioUtterance,
    Bye,
    Cancel,
    ClientInfo,
    CommandFrame,
    ErrorFrame,
    Feature,
    Heard,
    Hello,
    PartFrame,
    Ping,
    Pong,
    Ready,
    TurnEnded,
    TurnStatus,
    Utterance,
    VoiceChunk,
    VoiceEnd,
    VoiceStart,
    decode_edge_frame,
    encode_frame,
)
from factotum.contracts.errors import InvalidFrameError
from factotum.contracts.response import (
    CardPart,
    CommandPart,
    MediaPart,
    Speech,
    StatusPart,
    Text,
    required_capability,
)

CLIENT = ClientInfo(name="factotum-client-core", version="0.0.0")

_NOT_ACCEPTED = frozenset({"capability_missing", "speech_not_configured"})
"""The codes of an ``error`` frame that says an utterance was not taken at all: no ``turn_ended``
will follow it, so the wait for the answer ends with it."""


class Refused(Exception):  # noqa: N818 - it says what the edge did, not what failed
    """The edge did not accept the device, and said why (or did not)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class CannotConverse(Exception):  # noqa: N818
    """This device has no microphone, so it cannot start a conversation."""


@dataclass(slots=True)
class _Track:
    """The track of sound that an answer has begun, as far as the edge has sent it."""

    open: bool = False
    next_seq: int = 0


class DeviceClient:
    def __init__(
        self,
        connection: Connection,
        identity: Identity,
        renderer: Renderer,
        commands: CommandHandler,
        *,
        hardware_id: str | None = None,
        token: str | None = None,
        voice_output: VoiceOutput | None = None,
        voice_sink: VoiceSink | None = None,
        client: ClientInfo = CLIENT,
    ) -> None:
        self._connection = connection
        self._identity = identity
        self._renderer = renderer
        self._commands = commands
        self._hardware_id = hardware_id or identity.hardware_id
        self._token = token or identity.token
        self._voice_output = voice_output
        self._client = client
        self._player = (
            VoicePlayer(voice_sink, lambda why: renderer.notice(why))
            if voice_sink is not None
            else None
        )
        self._asks_for_voice = (
            voice_sink is not None and Capability.AUDIO_OUT in identity.capabilities
        )
        self._streams_voice = False
        """Whether the edge said it will send the sound as a stream."""

        self._heartbeat: asyncio.Task[None] | None = None
        self._turns: dict[str, asyncio.Event] = {}
        self._ended: dict[str, TurnEnded] = {}
        self._next_part: dict[str, int] = {}
        self._speech_parts: dict[str, set[int]] = {}
        self._tracks: dict[str, _Track] = {}
        self._abandoned: set[str] = set()
        """The answers that were spoken over: what is still on its way for them is let go of."""

        self._recordings: set[str] = set()
        self._heard: set[str] = set()
        self._counter = 0
        self.ready: Ready | None = None
        self.active: str | None = None
        """The utterance whose answer has not ended, if there is one."""

    @property
    def features(self) -> frozenset[Feature]:
        """What this client asks the edge for beyond version 1."""
        return frozenset({Feature.VOICE_STREAM}) if self._asks_for_voice else frozenset()

    # ---- saying hello --------------------------------------------------------------------------

    async def connect(self) -> Ready:
        """Say who this device is. Raises ``Refused`` when the edge does not accept it."""
        hello = Hello(
            protocol_version=PROTOCOL_VERSION,
            hardware_id=self._hardware_id,
            token=self._token,
            capabilities=self._identity.capabilities,
            display=self._identity.display,
            client=self._client,
            features=self.features,
        )
        try:
            await self._connection.send(encode_frame(hello))
            frame = decode_edge_frame(await self._connection.recv())
        except ConnectionLost as lost:
            raise Refused(
                "connection_lost", lost.reason or "the edge closed the connection"
            ) from None
        except InvalidFrameError as error:
            raise Refused(error.code, error.message) from None
        if isinstance(frame, ErrorFrame):
            raise Refused(frame.code, frame.message)
        if not isinstance(frame, Ready):
            raise Refused("protocol_error", "the edge did not say it was ready")
        self.ready = frame
        self._streams_voice = Feature.VOICE_STREAM in frame.features
        what = ", ".join(sorted(c.value for c in frame.capabilities)) or "nothing"
        self._renderer.notice(f"connected as {frame.name} ({what})")
        if frame.unregistered:
            ignored = ", ".join(sorted(c.value for c in frame.unregistered))
            self._renderer.notice(f"not registered for {ignored}: the edge will not use it")
        if frame.features - self.features:
            self._renderer.violation(
                "the edge will do what this device did not ask for: "
                + ", ".join(sorted(f.value for f in frame.features - self.features))
            )
        self._heartbeat = asyncio.create_task(self._keep_in_touch(frame.heartbeat_s))
        return frame

    async def _keep_in_touch(self, every_s: int) -> None:
        with contextlib.suppress(ConnectionLost):
            while True:
                await asyncio.sleep(every_s)
                await self._connection.send(encode_frame(Ping()))

    # ---- being spoken to -----------------------------------------------------------------------

    async def run(self) -> ConnectionLost | None:
        """Do what the edge sends, until the connection is over. Returns how it ended."""
        lost: ConnectionLost | None = None
        try:
            while True:
                text = await self._connection.recv()
                try:
                    frame = decode_edge_frame(text)
                except InvalidFrameError as error:
                    self._renderer.violation(f"the edge sent what is not a frame: {error.message}")
                    continue
                await self._handle(frame)
        except ConnectionLost as ended:
            lost = ended
        finally:
            if self._heartbeat is not None:
                self._heartbeat.cancel()
            for event in self._turns.values():
                event.set()  # nobody is left waiting for an answer that will not come
            self.active = None
            if self._player is not None:
                await self._player.stop()  # what is being said is for an answer that is gone
        return lost

    async def _handle(self, frame: object) -> None:
        match frame:
            case PartFrame():
                await self._part(frame)
            case VoiceStart():
                self._voice_start(frame)
            case VoiceChunk():
                self._voice_chunk(frame)
            case VoiceEnd():
                self._voice_end(frame)
            case Heard():
                self._what_was_heard(frame)
            case TurnEnded():
                self._renderer.turn_ended(frame)
                self._end(frame)
            case CommandFrame():
                result = await self._commands.execute(frame.command)
                with contextlib.suppress(ConnectionLost):
                    await self._connection.send(encode_frame(Ack(id=frame.id, result=result)))
            case ErrorFrame():
                self._renderer.error(frame)
                self._not_accepted(frame)
            case Pong():
                pass
            case Ready():
                self._renderer.violation("the edge said ready a second time")

    def _end(self, ended: TurnEnded) -> None:
        identity = ended.utterance_id
        track = self._tracks.pop(identity, None)
        if track is not None and track.open and identity not in self._abandoned:
            if ended.status is TurnStatus.FINISHED:
                self._renderer.violation("an answer ended with its sound not ended")
            if self._player is not None:
                self._player.end()  # what was received is played to its end
        self._abandoned.discard(identity)
        self._speech_parts.pop(identity, None)
        self._ended[identity] = ended
        self._next_part.pop(identity, None)
        if self.active == identity:
            self.active = None
        event = self._turns.get(identity)
        if event is not None:
            event.set()

    def _not_accepted(self, error: ErrorFrame) -> None:
        """The edge says it took no notice of an utterance (a device with no microphone, an edge
        with no speech): there will be no answer and no end of one, and what waits for it must not
        wait for ever. The wait ends as an answer that failed with the code the edge gave."""
        identity = error.ref
        if error.code not in _NOT_ACCEPTED or identity is None or identity not in self._next_part:
            return
        self._end(
            TurnEnded(
                utterance_id=identity,
                status=TurnStatus.FAILED,
                code=error.code,
                message=error.message,
            )
        )

    def _what_was_heard(self, heard: Heard) -> None:
        """What the edge took a recording for. It says so once, for a recording that is being
        answered, and before it begins to answer it."""
        identity = heard.utterance_id
        if identity not in self._recordings or identity not in self._next_part:
            self._renderer.violation(
                f"the edge said what it heard of {identity}, which is not a recording that is "
                "being answered"
            )
            return
        if identity in self._heard:
            self._renderer.violation(f"the edge said twice what it heard of {identity}")
            return
        self._heard.add(identity)
        if self._next_part[identity] > 0:
            self._renderer.violation("the edge said what it heard after it began to answer")
        self._renderer.heard(heard.text)

    async def _part(self, frame: PartFrame) -> None:
        expected = self._next_part.get(frame.utterance_id)
        if expected is None:
            self._renderer.violation(
                "the edge sent a part of an answer that nobody is waiting for "
                f"({frame.utterance_id})"
            )
            return
        if frame.seq != expected:
            self._renderer.violation(
                f"the parts of an answer came out of order: {expected} was expected, "
                f"{frame.seq} came"
            )
        self._next_part[frame.utterance_id] = frame.seq + 1

        part = frame.part
        needed = required_capability(part)
        if needed is not None and needed not in self._identity.capabilities:
            self._renderer.violation(
                f"the edge sent a {part.type} to a device that has no {needed.value}"
            )
            return
        match part:
            case Speech():
                self._renderer.speech(part.text)
                self._speech_parts.setdefault(frame.utterance_id, set()).add(frame.seq)
                if part.audio is not None:
                    await self._clip(part.audio)
            case Text():
                self._renderer.text(part.text)
            case CardPart():
                self._renderer.card(part.card)
            case MediaPart():
                await self._commands.execute(
                    PlayUrl(url=part.url, kind=part.kind, title=part.title)
                )
            case CommandPart():
                result = await self._commands.execute(part.command)
                if result.status is not CommandStatus.OK:
                    self._renderer.violation(f"a command in the answer failed: {result.detail}")
            case StatusPart():
                self._renderer.status(part.state)

    async def _clip(self, audio: AudioPayload) -> None:
        """The sound of a sentence, as a clip in its ``speech`` part."""
        if self._streams_voice:
            self._renderer.violation(
                "the edge sent a clip in a speech part to a device that takes its voice as a stream"
            )
        elif self._voice_output is not None:
            await self._voice_output.play(audio)

    # ---- the sound of an answer ----------------------------------------------------------------

    def _takes_sound_of(self, identity: str) -> bool:
        """Whether the sound that came for an answer is to be played: it is for an answer that is
        being given and was not spoken over. What comes for one that was is on its way still, and
        is let go of without a word; what comes for one that nobody waits for, or to a device that
        did not ask for it, is the edge's bug."""
        if not self._streams_voice or self._player is None:
            self._renderer.violation("the edge sent sound to a device that did not ask for it")
            return False
        if identity not in self._next_part:
            self._renderer.violation(
                f"the edge sent sound for an answer that nobody is waiting for ({identity})"
            )
            return False
        return identity not in self._abandoned

    def _voice_start(self, frame: VoiceStart) -> None:
        if not self._takes_sound_of(frame.utterance_id):
            return
        track = self._tracks.setdefault(frame.utterance_id, _Track())
        if track.open:
            self._renderer.violation("the edge began a track of sound while one was going on")
            return
        track.open, track.next_seq = True, 0
        assert self._player is not None  # noqa: S101 - checked by _takes_sound_of
        self._player.begin(frame.sample_rate)

    def _voice_chunk(self, frame: VoiceChunk) -> None:
        if not self._takes_sound_of(frame.utterance_id):
            return
        track = self._tracks.get(frame.utterance_id)
        if track is None or not track.open:
            self._renderer.violation("the edge sent a chunk of sound outside a track")
            return
        if frame.seq != track.next_seq:
            self._renderer.violation(
                f"the chunks of sound came out of order: {track.next_seq} was expected, "
                f"{frame.seq} came"
            )
        track.next_seq = frame.seq + 1
        if frame.part_seq not in self._speech_parts.get(frame.utterance_id, ()):
            self._renderer.violation(
                f"the edge sent the sound of part {frame.part_seq}, which is not a speech part "
                "that it sent"
            )
        assert self._player is not None  # noqa: S101 - checked by _takes_sound_of
        self._player.write(frame.data, frame.part_seq)

    def _voice_end(self, frame: VoiceEnd) -> None:
        if not self._takes_sound_of(frame.utterance_id):
            return
        track = self._tracks.get(frame.utterance_id)
        if track is None or not track.open:
            self._renderer.violation("the edge ended a track of sound that was not going on")
            return
        track.open = False
        assert self._player is not None  # noqa: S101 - checked by _takes_sound_of
        self._player.end()

    async def wait_until_played(self) -> None:
        """Everything of the sound that has been received is played, or given up on."""
        if self._player is not None:
            await self._player.idle()

    @property
    def playing(self) -> bool:
        """Whether sound that has been received is still being played."""
        return self._player is not None and self._player.playing

    # ---- speaking ------------------------------------------------------------------------------

    async def say(self, text: str, *, force: bool = False) -> str:
        """Say something to the assistant, and return what to call it. A device with no microphone
        cannot: ``force`` says it anyway, to see what the edge makes of it."""
        if not force:
            self._must_have_a_microphone()
        await self._speaking_over()
        identity = self._begin()
        await self._connection.send(encode_frame(Utterance(id=identity, text=text)))
        return identity

    async def say_audio(self, audio: AudioPayload) -> str:
        """Send a recording instead of typed text, and return what to call it. A device with no
        microphone cannot: the same ``CannotConverse`` as ``say``."""
        self._must_have_a_microphone()
        await self._speaking_over()
        identity = self._begin(recording=True)
        await self._connection.send(encode_frame(AudioUtterance(id=identity, audio=audio)))
        return identity

    async def say_audio_stream(self, stream: AudioStream) -> str:
        """Send what a person says as it is heard, instead of a recording made after it: an
        ``audio_start`` when they begin (which is when the edge stops the answer that is being
        given: that is speaking over it), a chunk for each piece the stream hands over, and an
        ``audio_end`` when it has no more, which is when they have stopped. Returns what to call
        it, once it is all sent, and does not wait for the answer (``wait_for_turn`` does). A device
        with no microphone cannot: the same ``CannotConverse`` as ``say``.

        What is playing stops before ``audio_start`` is sent. The edge may end the stream before the
        person has finished (it went on too long, or it stalled): it says so with a ``turn_ended``,
        and nothing more is sent after it."""
        self._must_have_a_microphone()
        await self._speaking_over()
        identity = self._begin(recording=True)
        await self._connection.send(
            encode_frame(AudioStart(id=identity, sample_rate=stream.sample_rate))
        )
        seq = 0
        async with contextlib.aclosing(stream.chunks):
            async for data in stream.chunks:
                if self._over(identity):
                    return identity
                await self._connection.send(
                    encode_frame(AudioChunk(id=identity, seq=seq, data=data))
                )
                seq += 1
        if not self._over(identity):
            await self._connection.send(encode_frame(AudioEnd(id=identity)))
        return identity

    def _must_have_a_microphone(self) -> None:
        if Capability.AUDIO_IN not in self._identity.capabilities:
            raise CannotConverse("this device has no microphone, so it cannot start a conversation")

    async def _speaking_over(self) -> None:
        """A person speaks while an answer plays: the answer is theirs to cut, and what plays stops
        on the spot, before the edge is told. What is still on its way for it is let go of."""
        if self.active is not None:
            self._abandoned.add(self.active)
        if self._player is not None:
            await self._player.stop()

    def _begin(self, *, recording: bool = False) -> str:
        """What the next thing said is called, and what is waited for of it."""
        self._counter += 1
        identity = f"u{self._counter}"
        self._turns[identity] = asyncio.Event()
        self._next_part[identity] = 0
        if recording:
            self._recordings.add(identity)
        self.active = identity
        return identity

    def _over(self, identity: str) -> bool:
        """Whether there is nothing more to say of an utterance: its turn has ended, or the
        connection has."""
        return self._turns[identity].is_set()

    async def wait_for_turn(self, identity: str) -> TurnEnded | None:
        """The answer to an utterance is over: how, or nothing if the connection ended first. The
        sound of it may still be playing (``wait_until_played``)."""
        event = self._turns.get(identity)
        if event is None:
            return None
        await event.wait()
        return self._ended.get(identity)

    async def cancel(self, identity: str | None = None) -> bool:
        """Stop the answer to an utterance (the one being answered, if none is named), and what
        plays of it. False when there is nothing being answered."""
        identity = identity or self.active
        if identity is None:
            return False
        self._abandoned.add(identity)
        if self._player is not None:
            await self._player.stop()
        await self._connection.send(encode_frame(Cancel(id=identity)))
        return True

    async def close(self) -> None:
        """Say goodbye and go."""
        with contextlib.suppress(ConnectionLost):
            await self._connection.send(encode_frame(Bye()))
        with contextlib.suppress(ConnectionLost):
            await self._connection.close()
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            await asyncio.wait({self._heartbeat})
        if self._player is not None:
            await self._player.stop()
