"""The device, put together and run: a key, a microphone and a speaker of a sound card that a test
plays, and an edge that a test plays over in-memory connections. What is run is the application and
the core under it, from the key that is pressed to the sound that comes out."""

import asyncio
import contextlib
import sys
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pytest

from factotum.client_audio import PushToTalk, Speaker, WakeWord
from factotum.client_audio.testing import FakeBackend, KeyPresses
from factotum.client_core import Ending, Refused
from factotum.client_core.audio import AudioStream
from factotum.client_core.testing import MemoryConnection, RecordingRenderer
from factotum.contracts.audio import wav_of
from factotum.contracts.capabilities import Capability
from factotum.contracts.commands import CommandStatus, SetVolume, Speak
from factotum.contracts.edge import (
    CloseCode,
    CommandFrame,
    ErrorFrame,
    Feature,
    PartFrame,
    TurnEnded,
    TurnStatus,
    VoiceChunk,
    VoiceEnd,
    VoiceStart,
)
from factotum.contracts.response import Speech
from factotum.device.app import build_hardware, identity_of, open_client, run_device
from factotum.device.config import ConfigError
from factotum.device.testing import Edge, a_config, ready, until

FRAME = 512
THE_VOICE = np.arange(1, 2001, dtype="<i2").tobytes()


def loud() -> Any:
    return np.full(FRAME, 1000, dtype=np.int16)


def quiet() -> Any:
    return np.zeros(FRAME, dtype=np.int16)


async def no_wait(seconds: float) -> None:
    """What a device waits between two tries: nothing, and what it was told to wait is kept."""
    waits.append(seconds)


waits: list[float] = []


@pytest.fixture(autouse=True)
def _forget_the_waits() -> None:
    waits.clear()


class TestWhoTheDeviceIs:
    def test_it_is_who_it_was_paired_as_and_has_what_it_declares(self) -> None:
        identity = identity_of(a_config(device={"capabilities": ["audio_out"]}))

        assert identity.hardware_id == "kitchen-01"
        assert identity.token == "kitchen-credential"
        assert identity.capabilities == {Capability.AUDIO_OUT}
        assert identity.display is None

    def test_its_credential_is_not_in_what_it_shows_of_itself(self) -> None:
        assert "kitchen-credential" not in repr(identity_of(a_config()))


class TestWhatItIsMadeOf:
    def test_a_speaker_with_a_microphone_is_a_key_and_a_speaker_of_the_sound_card(self) -> None:
        backend = FakeBackend()

        hardware = build_hardware(
            a_config(audio={"output_device": "USB", "volume": 40}),
            backend,
            RecordingRenderer(),
            presses=KeyPresses(),
        )

        assert isinstance(hardware.voice_input, PushToTalk)
        assert isinstance(hardware.speaker, Speaker)
        assert hardware.speaker.volume == 40
        assert backend.inputs == []  # nothing is opened to be built
        assert backend.outputs == []

    def test_with_a_word_it_is_the_word_that_it_waits_for(self) -> None:
        hardware = build_hardware(
            a_config(
                listening={"mode": "wake_word", "access_key": "a-key", "wake_word": "computer"}
            ),
            FakeBackend(),
            RecordingRenderer(),
        )

        assert isinstance(hardware.voice_input, WakeWord)

    def test_a_device_with_no_microphone_hears_nobody_and_one_with_no_speaker_says_nothing(
        self,
    ) -> None:
        no_microphone = build_hardware(
            a_config(device={"capabilities": ["audio_out"]}), FakeBackend(), RecordingRenderer()
        )
        no_speaker = build_hardware(
            a_config(device={"capabilities": ["audio_in"]}),
            FakeBackend(),
            RecordingRenderer(),
            presses=KeyPresses(),
        )

        assert (no_microphone.voice_input, no_microphone.speaker is None) == (None, False)
        assert (no_speaker.speaker, no_speaker.voice_input is None) == (None, False)

    def test_what_a_wake_word_cannot_do_without_is_refused_even_when_the_settings_were_changed(
        self,
    ) -> None:
        config = a_config(listening={"mode": "wake_word", "access_key": "a-key"})
        keyless = config.model_copy(
            update={"listening": config.listening.model_copy(update={"access_key": None})}
        )

        with pytest.raises(ConfigError, match="access key"):
            build_hardware(keyless, FakeBackend(), RecordingRenderer())

    def test_the_key_that_wakes_it_is_enter_unless_a_test_says_another(self) -> None:
        hardware = build_hardware(a_config(), FakeBackend(), RecordingRenderer())

        assert isinstance(hardware.voice_input, PushToTalk)


class TestSayingHello:
    async def test_a_device_with_a_speaker_asks_for_the_voice_as_a_stream(self) -> None:
        edge = Edge()
        edge.connection.say(ready())
        renderer = RecordingRenderer()
        hardware = build_hardware(a_config(), FakeBackend(), renderer, presses=KeyPresses())

        client = await open_client(a_config(), hardware, renderer, edge.open)

        [hello] = edge.connection.frames("hello")
        assert (hello.hardware_id, hello.token) == ("kitchen-01", "kitchen-credential")
        assert hello.capabilities == {Capability.AUDIO_IN, Capability.AUDIO_OUT}
        assert hello.features == {Feature.VOICE_STREAM}
        assert hello.client is not None
        await client.close()

    async def test_a_device_with_no_speaker_asks_for_no_feature(self) -> None:
        edge = Edge()
        config = a_config(device={"capabilities": ["audio_in"]})
        edge.connection.say(ready(frozenset({Capability.AUDIO_IN}), features=frozenset()))
        renderer = RecordingRenderer()
        hardware = build_hardware(config, FakeBackend(), renderer, presses=KeyPresses())

        client = await open_client(config, hardware, renderer, edge.open)

        [hello] = edge.connection.frames("hello")
        assert hello.features == frozenset()
        await client.close()

    async def test_an_edge_that_does_not_know_the_device_refuses_it_and_the_connection_is_closed(
        self,
    ) -> None:
        edge = Edge()
        edge.connection.say(ErrorFrame(code="auth_failed", message="who are you?", fatal=True))
        renderer = RecordingRenderer()
        hardware = build_hardware(a_config(), FakeBackend(), renderer, presses=KeyPresses())

        with pytest.raises(Refused, match="auth_failed"):
            await open_client(a_config(), hardware, renderer, edge.open)

        assert edge.connection.closed

    async def test_it_goes_where_the_settings_say(self) -> None:
        edge = Edge()
        edge.connection.say(ready())
        config = a_config(edge={"url": "wss://edge.example/v1/connect"})
        hardware = build_hardware(config, FakeBackend(), RecordingRenderer(), presses=KeyPresses())

        client = await open_client(config, hardware, RecordingRenderer(), edge.open)

        assert edge.asked == ["wss://edge.example/v1/connect"]
        await client.close()


class TestAConversation:
    async def say_to_it(
        self, presses: KeyPresses, backend: FakeBackend, edge: Edge, connection: int = 0
    ) -> str:
        """A person presses the key and says something, and what the device sent is what it is
        called."""
        presses.press()
        await until(lambda: backend.inputs and not backend.inputs[-1].closed)
        for block in [loud()] * 4 + [quiet()] * 6:
            backend.hearing.hear(block)
        [started] = await edge.connections[connection].wait_for("audio_start")
        await edge.connections[connection].wait_for("audio_end")
        return str(started.id)

    async def test_what_is_said_after_the_key_is_heard_and_the_answer_is_said_as_it_arrives(
        self,
    ) -> None:
        backend, presses, edge = FakeBackend(), KeyPresses(), Edge()
        renderer = RecordingRenderer()
        config = a_config(listening={"silence_seconds": 0.1})
        hardware = build_hardware(config, backend, renderer, presses=presses)
        edge.connection.say(ready())
        running = asyncio.create_task(run_device(config, hardware, renderer, opening=edge.open))

        said = await self.say_to_it(presses, backend, edge)
        connection = edge.connection
        connection.say(PartFrame(utterance_id=said, seq=0, part=Speech(text="Hello there.")))
        connection.say(VoiceStart(utterance_id=said, sample_rate=22_050))
        connection.say(VoiceChunk(utterance_id=said, seq=0, part_seq=0, data=THE_VOICE[:1000]))
        connection.say(VoiceChunk(utterance_id=said, seq=1, part_seq=0, data=THE_VOICE[1000:]))
        connection.say(VoiceEnd(utterance_id=said))
        connection.say(TurnEnded(utterance_id=said, status=TurnStatus.FINISHED))
        await until(lambda: backend.outputs and "stop" in backend.outputs[0].calls)
        presses.leave()
        outcome = await asyncio.wait_for(running, 2)

        assert outcome.ending is Ending.LEFT
        [output] = backend.outputs
        assert output.sample_rate == 22_050
        assert output.sound == THE_VOICE  # all of it, at the volume of the speaker (the full)
        assert renderer.of("speech") == ["Hello there."]
        assert renderer.of("violation") == []
        [microphone] = backend.inputs
        assert microphone.closed  # it was open for what was said and not after
        assert len(connection.frames("bye")) == 1

    async def test_a_speaker_that_was_turned_down_says_the_answer_quieter(self) -> None:
        backend, presses, edge = FakeBackend(), KeyPresses(), Edge()
        renderer = RecordingRenderer()
        config = a_config(listening={"silence_seconds": 0.1}, audio={"volume": 50})
        hardware = build_hardware(config, backend, renderer, presses=presses)
        edge.connection.say(ready())
        running = asyncio.create_task(run_device(config, hardware, renderer, opening=edge.open))
        said = await self.say_to_it(presses, backend, edge)
        edge.connection.say(PartFrame(utterance_id=said, seq=0, part=Speech(text="Hi.")))
        edge.connection.say(VoiceStart(utterance_id=said, sample_rate=22_050))
        edge.connection.say(
            VoiceChunk(
                utterance_id=said, seq=0, part_seq=0, data=np.array([1000, -1000], "<i2").tobytes()
            )
        )
        edge.connection.say(VoiceEnd(utterance_id=said))
        edge.connection.say(TurnEnded(utterance_id=said, status=TurnStatus.FINISHED))
        await until(lambda: backend.outputs and "stop" in backend.outputs[0].calls)

        presses.leave()
        await asyncio.wait_for(running, 2)

        assert np.frombuffer(backend.outputs[0].sound, "<i2").tolist() == [250, -250]

    async def test_the_person_leaving_is_how_it_ends_and_the_edge_is_said_goodbye(self) -> None:
        backend, presses, edge = FakeBackend(), KeyPresses(), Edge()
        config = a_config()
        renderer = RecordingRenderer()
        hardware = build_hardware(config, backend, renderer, presses=presses)
        edge.connection.say(ready())
        running = asyncio.create_task(run_device(config, hardware, renderer, opening=edge.open))
        await edge.connection.wait_for("hello")

        presses.leave()
        outcome = await asyncio.wait_for(running, 2)

        assert outcome.succeeded
        assert len(edge.connection.frames("bye")) == 1

    async def test_a_person_who_pressed_the_key_is_told_that_it_listens(self) -> None:
        backend, presses, edge = FakeBackend(), KeyPresses(), Edge()
        renderer = RecordingRenderer()
        config = a_config(listening={"silence_seconds": 0.1})
        hardware = build_hardware(config, backend, renderer, presses=presses)
        edge.connection.say(ready())
        running = asyncio.create_task(run_device(config, hardware, renderer, opening=edge.open))

        said = await self.say_to_it(presses, backend, edge)
        edge.connection.say(TurnEnded(utterance_id=said, status=TurnStatus.FINISHED))
        presses.leave()
        await asyncio.wait_for(running, 2)

        assert "listening" in renderer.of("notice")

    async def test_it_hears_and_speaks_with_the_devices_that_the_settings_choose(self) -> None:
        backend, presses, edge = FakeBackend(), KeyPresses(), Edge()
        renderer = RecordingRenderer()
        config = a_config(
            listening={"silence_seconds": 0.1},
            audio={"input_device": "USB mic", "output_device": 4},
        )
        hardware = build_hardware(config, backend, renderer, presses=presses)
        edge.connection.say(ready())
        running = asyncio.create_task(run_device(config, hardware, renderer, opening=edge.open))
        said = await self.say_to_it(presses, backend, edge)
        edge.connection.say(PartFrame(utterance_id=said, seq=0, part=Speech(text="Hi.")))
        edge.connection.say(VoiceStart(utterance_id=said, sample_rate=22_050))
        edge.connection.say(VoiceChunk(utterance_id=said, seq=0, part_seq=0, data=THE_VOICE))
        edge.connection.say(VoiceEnd(utterance_id=said))
        edge.connection.say(TurnEnded(utterance_id=said, status=TurnStatus.FINISHED))
        await until(lambda: backend.outputs and "stop" in backend.outputs[0].calls)

        presses.leave()
        await asyncio.wait_for(running, 2)

        assert backend.inputs[0].device == "USB mic"
        assert backend.outputs[0].device == 4

    async def test_a_clip_is_played_when_the_edge_does_not_stream_the_voice(self) -> None:
        backend, presses, edge = FakeBackend(), KeyPresses(), Edge()
        renderer = RecordingRenderer()
        config = a_config(listening={"silence_seconds": 0.1})
        hardware = build_hardware(config, backend, renderer, presses=presses)
        edge.connection.say(ready(features=frozenset()))
        running = asyncio.create_task(run_device(config, hardware, renderer, opening=edge.open))
        said = await self.say_to_it(presses, backend, edge)
        clip = Speech(text="Hello there.", audio=wav_of(THE_VOICE, 22_050))
        edge.connection.say(PartFrame(utterance_id=said, seq=0, part=clip))
        edge.connection.say(TurnEnded(utterance_id=said, status=TurnStatus.FINISHED))
        await until(lambda: backend.outputs and "stop" in backend.outputs[0].calls)

        presses.leave()
        await asyncio.wait_for(running, 2)

        assert backend.outputs[0].sound == THE_VOICE
        assert renderer.of("violation") == []

    async def test_it_waits_for_the_answer_before_it_listens_again(self) -> None:
        backend, presses, edge = FakeBackend(), KeyPresses(), Edge()
        renderer = RecordingRenderer()
        config = a_config(listening={"silence_seconds": 0.1})
        hardware = build_hardware(config, backend, renderer, presses=presses)
        edge.connection.say(ready())
        running = asyncio.create_task(run_device(config, hardware, renderer, opening=edge.open))
        await self.say_to_it(presses, backend, edge)  # and the answer is not given yet

        presses.press()  # a speaker that hears itself is not listened to while it answers
        await asyncio.sleep(0.2)

        assert len(backend.inputs) == 1
        assert len(edge.connection.frames("audio_start")) == 1
        running.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await running

    async def test_a_device_that_does_not_hear_itself_is_listened_to_while_it_answers(
        self,
    ) -> None:
        backend, presses, edge = FakeBackend(), KeyPresses(), Edge()
        renderer = RecordingRenderer()
        config = a_config(listening={"silence_seconds": 0.1, "listen_while_answering": True})
        hardware = build_hardware(config, backend, renderer, presses=presses)
        edge.connection.say(ready())
        running = asyncio.create_task(run_device(config, hardware, renderer, opening=edge.open))
        await self.say_to_it(presses, backend, edge)  # and the answer is not given yet

        presses.press()  # speaking over it
        await until(lambda: len(backend.inputs) == 2 and not backend.inputs[-1].closed)
        for block in [loud()] * 4 + [quiet()] * 6:
            backend.hearing.hear(block)
        await edge.connection.wait_for("audio_start", count=2)

        running.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await running


class TestStayingConnected:
    async def test_a_device_that_loses_the_edge_asks_again_and_goes_on_listening(self) -> None:
        backend, presses = FakeBackend(), KeyPresses()
        first, second = MemoryConnection(), MemoryConnection()
        edge = Edge(first, second)
        renderer = RecordingRenderer()
        config = a_config(listening={"silence_seconds": 0.1})
        hardware = build_hardware(config, backend, renderer, presses=presses)
        first.say(ready())
        second.say(ready())
        running = asyncio.create_task(
            run_device(
                config, hardware, renderer, opening=edge.open, sleep=no_wait, chance=lambda: 0.5
            )
        )
        await first.wait_for("hello")

        first.drop(1006)
        await second.wait_for("hello")
        presses.press()  # said to the connection that is there now
        await until(lambda: backend.inputs and not backend.inputs[-1].closed)
        for block in [loud()] * 4 + [quiet()] * 6:
            backend.hearing.hear(block)
        [started] = await second.wait_for("audio_start")
        await second.wait_for("audio_end")
        second.say(TurnEnded(utterance_id=started.id, status=TurnStatus.FINISHED))
        presses.leave()
        outcome = await asyncio.wait_for(running, 2)

        assert outcome.ending is Ending.LEFT
        assert len(edge.asked) == 2
        assert first.frames("audio_start") == []
        assert waits == [pytest.approx(0.01)]
        assert any("trying again" in what for what in renderer.of("notice"))

    async def test_an_edge_that_cannot_be_reached_is_asked_again_until_it_can(self) -> None:
        edge = Edge(failing=2)
        edge.connection.say(ready())
        presses, renderer = KeyPresses(), RecordingRenderer()
        config = a_config()
        hardware = build_hardware(config, FakeBackend(), renderer, presses=presses)
        running = asyncio.create_task(
            run_device(
                config, hardware, renderer, opening=edge.open, sleep=no_wait, chance=lambda: 0.5
            )
        )

        await until(lambda: len(edge.asked) == 3)
        await edge.connection.wait_for("hello")
        presses.leave()
        outcome = await asyncio.wait_for(running, 2)

        assert outcome.succeeded
        assert len(waits) == 2
        assert waits[1] == pytest.approx(0.02)  # twice as long, up to what the settings allow

    async def test_a_device_that_the_edge_does_not_accept_does_not_ask_again(self) -> None:
        edge = Edge()
        edge.connection.say(ErrorFrame(code="auth_failed", message="who are you?", fatal=True))
        renderer = RecordingRenderer()
        hardware = build_hardware(a_config(), FakeBackend(), renderer, presses=KeyPresses())

        outcome = await asyncio.wait_for(
            run_device(a_config(), hardware, renderer, opening=edge.open, sleep=no_wait), 2
        )

        assert outcome.ending is Ending.REFUSED
        assert "auth_failed" in outcome.detail
        assert len(edge.asked) == 1

    async def test_a_device_that_was_replaced_by_its_twin_does_not_take_its_place_back(
        self,
    ) -> None:
        edge = Edge()
        edge.connection.say(ready())
        edge.connection.drop(int(CloseCode.REPLACED), "the same device connected again")
        hardware = build_hardware(
            a_config(), FakeBackend(), RecordingRenderer(), presses=KeyPresses()
        )

        outcome = await asyncio.wait_for(
            run_device(a_config(), hardware, RecordingRenderer(), opening=edge.open, sleep=no_wait),
            2,
        )

        assert outcome.ending is Ending.REPLACED
        assert len(edge.asked) == 1


class TestADeviceThatIsSpokenThrough:
    """A speaker with no microphone: nothing starts a conversation there, and it does what the edge
    sends (a volume, a text to say) until the connection is over."""

    async def run_it(self, edge: Edge, backend: FakeBackend) -> tuple[Any, RecordingRenderer]:
        renderer = RecordingRenderer()
        config = a_config(device={"capabilities": ["audio_out"]})
        hardware = build_hardware(config, backend, renderer)
        task = asyncio.create_task(
            run_device(config, hardware, renderer, opening=edge.open, sleep=no_wait)
        )
        return task, renderer

    async def test_it_runs_the_commands_it_is_sent_and_says_what_became_of_each(self) -> None:
        backend, edge = FakeBackend(), Edge()
        edge.connection.say(ready(frozenset({Capability.AUDIO_OUT})))
        running, renderer = await self.run_it(edge, backend)
        await edge.connection.wait_for("hello")

        edge.connection.say(CommandFrame(id="c1", command=SetVolume(level=40)))
        edge.connection.say(CommandFrame(id="c2", command=Speak(text="dinner is ready")))
        acks = await edge.connection.wait_for("ack", count=2)
        edge.connection.drop(int(CloseCode.REPLACED))
        outcome = await asyncio.wait_for(running, 2)

        assert [(ack.id, ack.result.status) for ack in acks] == [
            ("c1", CommandStatus.OK),
            ("c2", CommandStatus.UNSUPPORTED),
        ]
        assert outcome.ending is Ending.REPLACED
        assert renderer.of("event")[0] == "volume 40"
        assert any("no microphone" in what for what in renderer.of("notice"))

    async def test_what_it_is_not_told_to_listen_for_is_not_heard(self) -> None:
        backend, edge = FakeBackend(), Edge()
        edge.connection.say(ready(frozenset({Capability.AUDIO_OUT})))
        running, _ = await self.run_it(edge, backend)
        await edge.connection.wait_for("hello")

        edge.connection.drop(int(CloseCode.REPLACED))
        await asyncio.wait_for(running, 2)

        assert backend.inputs == []  # no microphone was opened, ever
        assert len(edge.connection.frames("bye")) == 1  # but the edge was said goodbye


class Engine:
    """What ``pvporcupine.create`` makes: a frame of a level over 5000 has the word in it."""

    sample_rate = 16_000
    frame_length = FRAME

    def process(self, frame: Any) -> int:
        return 0 if int(frame[0]) > 5000 else -1

    def delete(self) -> None:
        """Nothing to let go of."""


class TestHowItIsWoken:
    async def test_the_word_its_key_and_the_microphone_are_those_of_the_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        made: list[dict[str, Any]] = []

        def create(**settings: Any) -> Engine:
            made.append(settings)
            return Engine()

        monkeypatch.setitem(sys.modules, "pvporcupine", SimpleNamespace(create=create))
        backend = FakeBackend()
        config = a_config(
            listening={"mode": "wake_word", "access_key": "a-key", "wake_word": "computer"},
            audio={"input_device": "USB mic"},
        )
        hardware = build_hardware(config, backend, RecordingRenderer())
        assert hardware.voice_input is not None
        waiting = asyncio.ensure_future(anext(hardware.voice_input.listen()))
        await until(lambda: backend.inputs)

        assert made == [{"access_key": "a-key", "keywords": ["computer"]}]
        assert backend.inputs[0].device == "USB mic"
        waiting.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiting

    @pytest.mark.parametrize("mode", ["push_to_talk", "wake_word"])
    @pytest.mark.parametrize(
        ("settings", "frames", "taken"),
        [
            pytest.param(
                {"silence_seconds": 5.0, "max_seconds": 0.2}, [loud()] * 20, 6, id="the longest"
            ),
            pytest.param(
                {"silence_seconds": 0.2, "silence_level": 2_000},
                [loud()] * 20,
                6,
                id="the level under which it is quiet",
            ),
            pytest.param(
                {"silence_seconds": 0.1},
                [loud()] + [quiet()] * 10,
                4,
                id="how long a person is quiet",
            ),
        ],
    )
    async def test_when_a_person_is_done_is_what_the_settings_say_whichever_way_it_is_woken(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
        settings: dict[str, Any],
        frames: list[Any],
        taken: int,
    ) -> None:
        monkeypatch.setitem(
            sys.modules, "pvporcupine", SimpleNamespace(create=lambda **given: Engine())
        )
        backend, presses = FakeBackend(), KeyPresses()
        woken = {"mode": "wake_word", "access_key": "a-key"} if mode == "wake_word" else {}
        config = a_config(listening={**woken, **settings})
        hardware = build_hardware(config, backend, RecordingRenderer(), presses=presses)
        assert hardware.voice_input is not None
        heard = cast("AsyncGenerator[Any]", hardware.voice_input.listen())
        waiting = asyncio.ensure_future(anext(heard))
        if mode == "wake_word":
            await until(lambda: backend.inputs)
            backend.hearing.hear(np.full(FRAME, 6000, dtype=np.int16))  # the word
        else:
            presses.press()
        stream = await asyncio.wait_for(waiting, 2)
        assert isinstance(stream, AudioStream)
        chunks: list[bytes] = []

        async def read() -> None:
            chunks.extend([chunk async for chunk in stream.chunks])

        reading = asyncio.create_task(read())
        for frame in frames:
            await asyncio.sleep(0)
            backend.hearing.hear(frame)
        await asyncio.wait_for(reading, 2)
        await heard.aclose()

        assert len(b"".join(chunks)) // 2 // FRAME == taken
