"""What the doctor says is wrong, with a sound card and an edge that a test plays: each check by
itself, what it says when it fails and what to do about it, and that it makes no sound and hears
nothing unless it was asked to."""

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from factotum.client_audio.backend import AudioDevice
from factotum.client_audio.testing import MICROPHONE, SPEAKER, FakeBackend, FakeWakeWord
from factotum.client_audio.wakeword import OpenWakeWordEngine, WakeWordEngine
from factotum.client_core.testing import MemoryConnection
from factotum.contracts.capabilities import Capability
from factotum.contracts.edge import ErrorFrame, Feature
from factotum.device import doctor
from factotum.device.app import EngineFactory, engine_of
from factotum.device.config import Config, ListeningSettings
from factotum.device.doctor import (
    SPEAKER_RATE,
    Check,
    check_listening,
    check_playing,
    check_wake_word,
    check_word_heard,
    examine,
    report_of,
    tone,
)
from factotum.device.testing import Edge, a_config, ready, until

NAMES = ["settings", "sound devices", "microphone", "speaker", "edge"]


def an_engine(score: float | Callable[[Any], float] = 0.0) -> EngineFactory:
    """What makes an engine whose model of the word says ``score``, for each engine that is asked
    for (the models of openWakeWord are made up, and the sound card is the test's)."""
    return lambda listening: lambda: FakeWakeWord(score).engine


class Closing(OpenWakeWordEngine):
    """An engine that says that it was let go of."""

    closed = False

    def close(self) -> None:
        self.closed = True


class Slow(Closing):
    """An engine that takes sound at a rate that is not the one that a microphone is opened at when
    nobody says."""

    sample_rate = 8_000


def engines_that_say_when_they_are_closed(
    made: list[Closing],
    kind: type[Closing] = Closing,
    score: float | Callable[[Any], float] = 0.0,
) -> EngineFactory:
    """What makes an engine of this kind for each that is asked for, and keeps it in ``made``."""

    def engine(listening: ListeningSettings) -> Callable[[], WakeWordEngine]:
        def make() -> WakeWordEngine:
            models = FakeWakeWord(score)
            made.append(kind(models.melspectrogram, models.embedding, models.word))
            return made[-1]

        return make

    return engine


def a_loud_sound_is_the_word(score: float = 0.9) -> EngineFactory:
    """An engine for which the word is any frame that is loud: the made-up models answer with the
    mean of what they are given (silence is an embedding of 2, a loud frame one of about 48)."""
    return an_engine(lambda features: score if features[0, -1, 0] > 20 else 0.0)


async def examined(
    config: Config | None = None,
    *,
    edge: Edge | None = None,
    backend: FakeBackend | None = None,
    listen: bool = False,
    play: bool = False,
    word: bool = False,
    engine: EngineFactory | None = None,
) -> list[Check]:
    """The checks of a device, against an edge that says ready unless the test made another."""
    if edge is None:
        edge = Edge()
        edge.connection.say(ready())
    return await examine(
        config or a_config(),
        backend or FakeBackend(),
        listen=listen,
        play=play,
        word=word,
        opening=edge.open,
        engine=engine or an_engine(),
    )


def by_name(checks: list[Check]) -> dict[str, Check]:
    return {check.name: check for check in checks}


class TestADeviceThatIsAllRight:
    async def test_every_check_is_ok_and_each_says_what_it_found(self) -> None:
        checks = await examined()

        assert [check.name for check in checks] == NAMES
        assert all(check.ok for check in checks)
        found = by_name(checks)
        assert "kitchen-01" in found["settings"].detail
        assert "push_to_talk" in found["settings"].detail
        assert "0: Fake microphone" in found["sound devices"].detail
        assert "default microphone" in found["sound devices"].detail
        assert "default speaker" in found["sound devices"].detail
        assert (
            found["edge"].detail == "connected as Kitchen speaker; the edge will use: voice_stream"
        )

    async def test_it_makes_no_sound_and_hears_nothing_unless_it_is_asked_to(self) -> None:
        backend = FakeBackend()

        await examined(backend=backend)

        assert backend.inputs == []
        assert backend.outputs == []

    async def test_it_tries_the_devices_that_were_chosen_at_the_rates_they_will_be_used_at(
        self,
    ) -> None:
        backend = FakeBackend()
        config = a_config(audio={"input_device": "USB mic", "output_device": 4})

        await examined(config, backend=backend)

        assert backend.checked == [
            ("input", "USB mic", 16_000),  # what Whisper and the wake word hear at
            ("output", 4, SPEAKER_RATE),  # what the voice is made at
        ]

    async def test_it_connects_as_the_device_and_says_goodbye(self) -> None:
        edge = Edge()
        edge.connection.say(ready())

        await examined(edge=edge)

        [hello] = edge.connection.frames("hello")
        assert hello.hardware_id == "kitchen-01"
        assert len(edge.connection.frames("bye")) == 1

    async def test_it_need_not_connect_to_the_edge_which_would_take_the_place_of_a_device(
        self,
    ) -> None:
        edge = Edge()

        checks = await examine(a_config(), FakeBackend(), edge=False, opening=edge.open)

        assert [check.name for check in checks] == [
            "settings",
            "sound devices",
            "microphone",
            "speaker",
        ]
        assert edge.asked == []

    async def test_a_wake_word_is_in_what_it_says_of_the_settings(self) -> None:
        config = a_config(listening={"mode": "wake_word"})

        checks = await examined(config)

        assert "wake_word ('hey_jarvis', openwakeword)" in by_name(checks)["settings"].detail

    async def test_and_so_is_the_word_of_porcupine_and_never_its_key(self) -> None:
        config = a_config(
            listening={"mode": "wake_word", "engine": "porcupine", "access_key": "a-key"}
        )

        checks = await examined(config)

        assert "wake_word ('jarvis', porcupine)" in by_name(checks)["settings"].detail
        assert "a-key" not in by_name(checks)["settings"].detail

    async def test_an_edge_that_will_send_clips_and_not_a_stream_is_said_to(self) -> None:
        edge = Edge()
        edge.connection.say(ready(features=frozenset()))

        checks = await examined(edge=edge)

        assert "a clip for each sentence" in by_name(checks)["edge"].detail


class TestWhatDependsOnWhatTheDeviceHas:
    async def test_a_device_with_no_microphone_is_not_asked_whether_it_hears(self) -> None:
        config = a_config(device={"capabilities": ["audio_out"]})
        edge = Edge()
        edge.connection.say(ready(frozenset({Capability.AUDIO_OUT})))

        checks = await examined(config, edge=edge)

        assert [check.name for check in checks] == ["settings", "sound devices", "speaker", "edge"]

    async def test_and_one_with_no_speaker_is_not_asked_whether_it_plays(self) -> None:
        config = a_config(device={"capabilities": ["audio_in"]})
        edge = Edge()
        edge.connection.say(ready(frozenset({Capability.AUDIO_IN}), features=frozenset()))

        checks = await examined(config, edge=edge)

        assert [check.name for check in checks] == [
            "settings",
            "sound devices",
            "microphone",
            "edge",
        ]


class TestWhatTheEdgeSaysOfTheDevice:
    async def test_a_device_that_declares_what_it_is_not_registered_for_is_told_so(self) -> None:
        edge = Edge()
        edge.connection.say(
            ready(frozenset({Capability.AUDIO_OUT}), unregistered=frozenset({Capability.AUDIO_IN}))
        )

        checks = by_name(await examined(edge=edge))

        assert not checks["edge"].ok
        assert "not registered for audio_in" in checks["edge"].detail
        assert "device.capabilities" in (checks["edge"].advice or "")

    async def test_an_edge_that_does_what_the_device_did_not_ask_for_is_a_bug_that_is_said(
        self,
    ) -> None:
        edge = Edge()
        config = a_config(device={"capabilities": ["audio_in"]})
        edge.connection.say(
            ready(frozenset({Capability.AUDIO_IN}), features=frozenset({Feature.VOICE_STREAM}))
        )

        checks = by_name(await examined(config, edge=edge))

        assert not checks["edge"].ok
        assert "did not ask for" in checks["edge"].detail
        assert "bug of the edge" in (checks["edge"].advice or "")


class TestWhatIsWrong:
    async def test_a_machine_that_lists_no_microphone_is_one_that_cannot_hear(self) -> None:
        backend = FakeBackend([SPEAKER], default_input=None)

        checks = by_name(await examined(backend=backend))

        assert not checks["sound devices"].ok
        assert "microphone or a speaker" in (checks["sound devices"].advice or "")

    async def test_a_machine_that_lists_no_speaker_is_one_that_cannot_speak(self) -> None:
        backend = FakeBackend([MICROPHONE], default_output=None)

        checks = by_name(await examined(backend=backend))

        assert not checks["sound devices"].ok
        assert "microphone or a speaker" in (checks["sound devices"].advice or "")

    async def test_a_machine_that_cannot_list_its_devices_says_what_to_install(self) -> None:
        backend = FakeBackend()
        backend.refuses["devices"] = "PortAudio could not be loaded"

        checks = by_name(await examined(backend=backend))

        assert not checks["sound devices"].ok
        assert "libportaudio2" in (checks["sound devices"].advice or "")

    async def test_a_microphone_that_cannot_be_used_is_said_with_the_privacy_setting_of_windows(
        self,
    ) -> None:
        backend = FakeBackend()
        backend.refuses["check_input"] = "Invalid number of channels"

        checks = by_name(await examined(backend=backend))

        assert not checks["microphone"].ok
        assert checks["microphone"].detail == "Invalid number of channels"
        assert "Privacy" in (checks["microphone"].advice or "")
        assert checks["speaker"].ok  # what is wrong is one thing at a time

    async def test_a_speaker_that_does_not_take_the_rate_of_the_voice_is_said(self) -> None:
        backend = FakeBackend()
        backend.refuses["check_output"] = "Invalid sample rate"

        checks = by_name(await examined(backend=backend))

        assert not checks["speaker"].ok
        assert "audio.output_device" in (checks["speaker"].advice or "")

    async def test_an_edge_that_cannot_be_reached_is_said_with_where_it_was_looked_for(
        self,
    ) -> None:
        checks = by_name(await examined(edge=Edge(failing=1)))

        assert not checks["edge"].ok
        assert "connection refused" in checks["edge"].detail
        assert "ws://127.0.0.1:8084/v1/connect" in (checks["edge"].advice or "")

    async def test_an_edge_that_does_not_know_the_device_says_which_of_its_settings_to_look_at(
        self,
    ) -> None:
        edge = Edge()
        edge.connection.say(ErrorFrame(code="auth_failed", message="who are you?", fatal=True))

        checks = by_name(await examined(edge=edge))

        assert not checks["edge"].ok
        assert "auth_failed" in checks["edge"].detail
        assert "hardware_id" in (checks["edge"].advice or "")
        assert edge.connection.closed

    async def test_a_refusal_that_it_has_no_advice_for_sends_a_person_to_the_logs_of_the_edge(
        self,
    ) -> None:
        edge = Edge()
        edge.connection.say(ErrorFrame(code="something_new", message="no", fatal=True))

        checks = by_name(await examined(edge=edge))

        assert "logs why" in (checks["edge"].advice or "")


class TestHearing:
    async def test_a_microphone_that_hears_something_says_how_loud(self) -> None:
        backend, config = FakeBackend(), a_config()
        checking = asyncio.create_task(check_listening(config, backend))
        await until(lambda: backend.inputs)
        for _ in range(70):
            backend.inputs[0].hear(np.full(512, 500, dtype=np.int16))

        check = await asyncio.wait_for(checking, 2)

        assert check.ok
        assert "mean level 500" in check.detail
        assert backend.inputs[0].closed

    async def test_a_microphone_that_heard_one_loud_sound_is_not_a_dead_one(self) -> None:
        backend = FakeBackend()
        checking = asyncio.create_task(check_listening(a_config(), backend))
        await until(lambda: backend.inputs)
        for level in [0] * 61 + [800] + [0] * 8:  # 62 blocks are heard, and one of them is a click
            backend.inputs[0].hear(np.full(512, level, dtype=np.int16))

        check = await asyncio.wait_for(checking, 2)

        assert check.ok
        assert "loudest 800" in check.detail

    async def test_a_microphone_that_hears_nothing_is_one_that_is_muted_or_not_the_one(
        self,
    ) -> None:
        backend = FakeBackend()
        checking = asyncio.create_task(check_listening(a_config(), backend))
        await until(lambda: backend.inputs)
        for _ in range(70):
            backend.inputs[0].hear(np.zeros(512, dtype=np.int16))

        check = await asyncio.wait_for(checking, 2)

        assert not check.ok
        assert "muted" in (check.advice or "")

    async def test_a_microphone_that_hands_over_nothing_at_all_is_dead(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(doctor, "BLOCK_WAIT_S", 0.05)

        check = await check_listening(a_config(), FakeBackend())

        assert not check.ok
        assert check.detail == "nothing came from the microphone"

    async def test_a_microphone_that_cannot_be_opened_is_the_error_of_the_backend(self) -> None:
        backend = FakeBackend()
        backend.refuses["open_input"] = "in use"

        check = await check_listening(a_config(), backend)

        assert (check.ok, check.detail) == (False, "in use")

    async def test_it_listens_only_when_asked_and_only_if_the_microphone_is_there(self) -> None:
        backend = FakeBackend()
        backend.refuses["check_input"] = "no"

        checks = await examined(backend=backend, listen=True)

        assert "listening" not in by_name(checks)
        assert backend.inputs == []

    async def test_asked_to_listen_it_does_and_says_so(self) -> None:
        backend = FakeBackend()
        edge = Edge()
        edge.connection.say(ready())
        checking = asyncio.create_task(examine(a_config(), backend, listen=True, opening=edge.open))
        await until(lambda: backend.inputs)
        for _ in range(70):
            backend.inputs[0].hear(np.full(512, 300, dtype=np.int16))

        checks = await asyncio.wait_for(checking, 2)

        assert [check.name for check in checks][-1] == "listening"
        assert checks[-1].ok


class TestPlaying:
    async def test_a_tone_is_played_quietly_and_a_person_is_asked_if_they_heard_it(self) -> None:
        backend = FakeBackend()
        config = a_config(audio={"volume": 50})
        edge = Edge()
        edge.connection.say(ready())

        checks = by_name(await examine(config, backend, play=True, opening=edge.open))

        assert checks["playing"].ok
        assert "did you hear it" in checks["playing"].detail
        [output] = backend.outputs
        assert output.sample_rate == SPEAKER_RATE
        samples = np.frombuffer(output.sound, dtype="<i2")
        assert 0 < int(np.abs(samples).max()) <= 6000 // 4  # at the volume of the speaker

    async def test_a_speaker_that_cannot_play_it_says_so(self) -> None:
        backend = FakeBackend()
        backend.refuses["open_output"] = "no such device"
        from factotum.client_audio import Speaker

        check = await check_playing(a_config(), Speaker(backend))

        assert (check.ok, check.detail) == (False, "no such device")

    async def test_a_device_with_no_speaker_plays_nothing_even_when_asked(self) -> None:
        backend = FakeBackend()
        config = a_config(device={"capabilities": ["audio_in"]})
        edge = Edge()
        edge.connection.say(ready(frozenset({Capability.AUDIO_IN}), features=frozenset()))

        checks = await examine(config, backend, play=True, opening=edge.open)

        assert "playing" not in by_name(checks)
        assert backend.outputs == []


class TestTheTone:
    def test_it_is_as_long_as_it_was_asked_to_be_and_whole_samples(self) -> None:
        pcm = tone(rate=8000, seconds=0.5)

        assert len(pcm) == 8000 // 2 * 2

    def test_it_fades_in_and_out_so_that_it_does_not_click(self) -> None:
        samples = np.frombuffer(tone(), dtype="<i2")

        assert abs(int(samples[0])) < 100
        assert abs(int(samples[-1])) < 100
        assert int(np.abs(samples).max()) <= 6000

    def test_it_is_a_tone_and_not_silence(self) -> None:
        samples = np.frombuffer(tone(), dtype="<i2")

        assert int(np.abs(samples).max()) > 5000


class TestTheReport:
    def test_each_check_is_a_line_that_says_ok_or_fail_and_what_to_do(self) -> None:
        lines = report_of(
            [
                Check("settings", True, "kitchen-01 on ws://edge"),
                Check("microphone", False, "Invalid channels", "choose another"),
            ]
        ).splitlines()

        assert lines == [
            "ok    settings      kitchen-01 on ws://edge",
            "FAIL  microphone    Invalid channels",
            "                    -> choose another",
        ]

    def test_what_goes_on_for_several_lines_is_lined_up(self) -> None:
        lines = report_of([Check("sound devices", True, "0: a\n1: b")]).splitlines()

        assert lines == ["ok    sound devices 0: a", "                    1: b"]

    def test_advice_is_for_what_failed_and_not_for_what_is_all_right(self) -> None:
        assert "->" not in report_of([Check("edge", True, "connected", "never shown")])

    def test_devices_are_listed_with_what_each_is(self) -> None:
        listed = [AudioDevice(3, "Headset", 1, 2, 48_000.0, "WASAPI")]
        backend = FakeBackend(listed, default_input=3, default_output=None)

        found = asyncio.run(doctor.check_devices(a_config(), backend))

        assert found.detail == "3: Headset [WASAPI; in/out]  <- default microphone"


class TestAnEdgeThatTheTestsOwnConnectionStandsFor:
    async def test_the_connection_is_the_one_the_edge_gave(self) -> None:
        connection = MemoryConnection()
        connection.say(ready())

        checks = await examine(a_config(), FakeBackend(), opening=Edge(connection).open)

        assert by_name(checks)["edge"].ok


class TestTheWakeWord:
    async def test_a_device_that_a_key_wakes_is_not_asked_about_a_word(self) -> None:
        checks = await examined()

        assert "wake word" not in by_name(checks)

    async def test_one_that_waits_for_a_word_is_asked_whether_what_listens_for_it_can_be_had(
        self, tmp_path: Path
    ) -> None:
        config = a_config(listening={"mode": "wake_word", "models_dir": str(tmp_path)})

        checks = await examined(config)

        assert [check.name for check in checks] == [*NAMES[:4], "wake word", "edge"]
        found = by_name(checks)["wake word"]
        assert found.ok
        assert "'hey_jarvis' by openwakeword, threshold 0.5" in found.detail
        assert str(tmp_path) in found.detail

    async def test_models_that_are_not_there_are_said_with_the_command_that_brings_them(
        self, tmp_path: Path
    ) -> None:
        config = a_config(listening={"mode": "wake_word", "models_dir": str(tmp_path)})

        found = by_name(await examined(config, engine=engine_of))["wake word"]

        assert not found.ok
        assert "'hey_jarvis'" in found.detail
        for name in ("melspectrogram.onnx", "embedding_model.onnx", "hey_jarvis_v0.1.onnx"):
            assert name in found.detail
        assert str(tmp_path) in found.detail
        assert found.advice == f'factotum-device wake-word download hey_jarvis --dir "{tmp_path}"'

    async def test_the_command_does_not_name_the_folder_that_nobody_chose(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        config = a_config(listening={"mode": "wake_word", "wake_word": "alexa"})

        found = by_name(await examined(config, engine=engine_of))["wake word"]

        assert found.advice == "factotum-device wake-word download alexa"

    async def test_models_that_cannot_be_loaded_are_said_and_so_is_how_to_fetch_them_again(
        self, tmp_path: Path
    ) -> None:
        def engine(listening: ListeningSettings) -> Callable[[], WakeWordEngine]:
            def make() -> WakeWordEngine:
                raise RuntimeError("Protobuf parsing failed")

            return make

        config = a_config(listening={"mode": "wake_word", "models_dir": str(tmp_path)})

        found = by_name(await examined(config, engine=engine))["wake word"]

        assert not found.ok
        assert "the models of 'hey_jarvis' could not be loaded: Protobuf parsing failed" in (
            found.detail
        )
        assert found.advice == (
            f'fetch them again: factotum-device wake-word download hey_jarvis --dir "{tmp_path}"'
        )

    async def test_the_engine_that_was_made_to_see_that_it_can_be_is_let_go_of(self) -> None:
        made: list[Closing] = []
        config = a_config(listening={"mode": "wake_word"})

        found = await check_wake_word(config, engines_that_say_when_they_are_closed(made))

        assert found.ok
        [engine] = made
        assert engine.closed

    async def test_a_word_that_there_is_no_model_for_is_said_with_those_that_there_are(
        self,
    ) -> None:
        config = a_config(listening={"wake_word": "computer"})  # push to talk: it is not looked at

        found = by_name(await examined(config, word=True, engine=engine_of))["wake word"]

        assert not found.ok
        assert "no model for 'computer'" in found.detail
        assert "alexa, hey_jarvis, hey_mycroft, hey_rhasspy" in found.detail
        assert "listening.wake_word" in (found.advice or "")

    async def test_porcupine_needs_only_its_package_here_and_picovoice_checks_its_key_later(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "pvporcupine", SimpleNamespace())
        config = a_config(
            listening={"mode": "wake_word", "engine": "porcupine", "access_key": "a-key"}
        )

        found = by_name(await examined(config))["wake word"]

        assert found.ok
        assert "'jarvis' by porcupine" in found.detail
        assert "a-key" not in found.detail

    async def test_porcupine_without_its_package_is_said_with_how_to_have_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "pvporcupine", None)  # an import that fails
        config = a_config(
            listening={"mode": "wake_word", "engine": "porcupine", "access_key": "a-key"}
        )

        found = by_name(await examined(config))["wake word"]

        assert not found.ok
        assert "package is not installed" in found.detail
        assert "uv sync --all-packages --extra porcupine" in (found.advice or "")
        assert "openwakeword needs neither" in (found.advice or "")

    async def test_a_device_with_no_microphone_is_not_asked_about_a_word_even_when_told_to(
        self,
    ) -> None:
        config = a_config(device={"capabilities": ["audio_out"]})
        edge = Edge()
        edge.connection.say(ready(frozenset({Capability.AUDIO_OUT})))

        checks = await examined(config, edge=edge, word=True)

        assert "wake word" not in by_name(checks)
        assert "word" not in by_name(checks)

    async def test_it_can_be_asked_for_one_that_a_key_wakes_too(self) -> None:
        checks = await examined(word=True, engine=an_engine())

        assert "wake word" in by_name(checks)

    async def test_what_it_looks_for_is_what_the_settings_ask_for(self) -> None:
        asked: list[ListeningSettings] = []

        def engine(listening: ListeningSettings) -> Callable[[], WakeWordEngine]:
            asked.append(listening)
            return lambda: FakeWakeWord().engine

        config = a_config(listening={"mode": "wake_word", "wake_word": "alexa", "patience": 2})

        await check_wake_word(config, engine)

        assert asked == [config.listening]


class TestListeningForTheWord:
    """``--word``: five seconds of the microphone, and how near the word came."""

    async def heard(
        self,
        blocks: list[Any],
        *,
        engine: EngineFactory | None = None,
        config: Config | None = None,
        backend: FakeBackend | None = None,
        say: Callable[[str], None] = lambda line: None,
    ) -> Check:
        backend = backend or FakeBackend()
        checking = asyncio.create_task(
            check_word_heard(
                config or a_config(listening={"mode": "wake_word"}),
                backend,
                engine or a_loud_sound_is_the_word(),
                seconds=1.0,
                say=say,
            )
        )
        await until(lambda: backend.inputs)
        for block in blocks:
            backend.inputs[0].hear(block)
        return await asyncio.wait_for(checking, 2)

    @staticmethod
    def said(*, loud_at: int | None = None, blocks: int = 12) -> list[Any]:
        """A second of sound, of which one frame (or none) is loud."""
        return [FakeWakeWord.frame(6_000 if at == loud_at else 0) for at in range(blocks)]

    async def test_a_word_that_was_taken_is_said_with_the_score_it_came_to(self) -> None:
        check = await self.heard(self.said(loud_at=8))

        assert check.ok
        assert check.name == "word"
        assert "'hey_jarvis' was heard" in check.detail
        assert "best score in 1 s was 0.90 (threshold 0.5)" in check.detail

    async def test_a_score_that_is_exactly_the_threshold_is_the_word_as_it_is_for_the_engine(
        self,
    ) -> None:
        check = await self.heard(self.said(loud_at=8), engine=a_loud_sound_is_the_word(0.5))

        assert check.ok

    async def test_a_word_that_came_near_says_the_threshold_that_would_have_taken_it(self) -> None:
        check = await self.heard(self.said(loud_at=8), engine=a_loud_sound_is_the_word(0.3))

        assert not check.ok
        assert "'hey_jarvis' was not heard: the best score in 1 s was 0.30" in check.detail
        assert "under the threshold of 0.5" in check.detail
        assert "listening.threshold 0.24" in (check.advice or "")
        assert "lets in more sounds" in (check.advice or "")

    @pytest.mark.parametrize(("score", "near"), [(0.25, True), (0.1875, False)])
    async def test_from_a_quarter_of_the_way_it_came_near_and_under_it_it_did_not(
        self, score: float, near: bool
    ) -> None:
        check = await self.heard(self.said(loud_at=8), engine=a_loud_sound_is_the_word(score))

        assert ("it came near" in (check.advice or "")) is near
        assert ("it did not come near" in (check.advice or "")) is not near

    async def test_a_word_that_did_not_come_near_says_what_else_could_be_tried(self) -> None:
        check = await self.heard(self.said(), engine=a_loud_sound_is_the_word(0.9))

        assert not check.ok
        assert "was 0.00" in check.detail
        advice = check.advice or ""
        assert "nearer to the microphone" in advice
        assert "alexa, hey_mycroft, hey_rhasspy" in advice
        assert "hey_jarvis" not in advice  # it is the one that was tried

    async def test_the_person_is_told_when_to_speak_before_anything_is_heard(self) -> None:
        backend = FakeBackend()
        lines: list[tuple[str, int]] = []

        await self.heard(
            self.said(loud_at=8),
            backend=backend,
            say=lambda line: lines.append((line, len(backend.inputs))),
        )

        assert lines == [("say 'hey jarvis' now: listening for 1 seconds", 0)]

    async def test_the_microphone_is_the_settings_at_the_rate_and_the_frame_of_the_engine(
        self,
    ) -> None:
        backend = FakeBackend()
        config = a_config(listening={"mode": "wake_word"}, audio={"input_device": "USB mic"})

        await self.heard(self.said(loud_at=8), backend=backend, config=config)

        [microphone] = backend.inputs
        assert (microphone.device, microphone.sample_rate, microphone.block_size) == (
            "USB mic",
            16_000,
            1_280,
        )
        assert microphone.closed  # it was open for the word and not after

    async def test_the_last_frame_of_the_time_is_heard_too(self) -> None:
        check = await self.heard(self.said(loud_at=11, blocks=12))  # a second is 12 frames

        assert check.ok

    async def test_the_engine_is_let_go_of_when_it_has_listened(self) -> None:
        made: list[Closing] = []
        word = lambda features: 0.9 if features[0, -1, 0] > 20 else 0.0  # noqa: E731

        check = await self.heard(
            self.said(loud_at=8), engine=engines_that_say_when_they_are_closed(made, score=word)
        )

        assert check.ok
        [engine] = made
        assert engine.closed

    async def test_and_when_the_microphone_could_not_be_opened(self) -> None:
        made: list[Closing] = []
        backend = FakeBackend()
        backend.refuses["open_input"] = "in use"

        check = await check_word_heard(
            a_config(listening={"mode": "wake_word"}),
            backend,
            engines_that_say_when_they_are_closed(made),
            seconds=1.0,
        )

        assert (check.ok, check.detail) == (False, "in use")
        [engine] = made
        assert engine.closed

    async def test_the_microphone_is_opened_at_the_rate_that_the_engine_says(self) -> None:
        made: list[Closing] = []
        backend = FakeBackend()
        checking = asyncio.create_task(
            check_word_heard(
                a_config(listening={"mode": "wake_word"}),
                backend,
                engines_that_say_when_they_are_closed(made, Slow),
                seconds=1.0,
            )
        )
        await until(lambda: backend.inputs)

        [microphone] = backend.inputs
        assert (microphone.sample_rate, microphone.block_size) == (8_000, 1_280)
        for block in self.said(blocks=6):  # a second is 6 frames of 160 ms at that rate
            microphone.hear(block)
        await asyncio.wait_for(checking, 2)

    async def test_it_listens_for_as_long_as_it_says_and_not_a_frame_more(self) -> None:
        # A second is 12 frames of 80 ms: a loud one that comes after them is not heard.
        check = await self.heard(self.said(loud_at=12, blocks=14))

        assert not check.ok
        assert "was 0.00" in check.detail

    async def test_a_microphone_that_hands_over_nothing_is_said(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(doctor, "BLOCK_WAIT_S", 0.05)

        check = await self.heard([])

        assert (check.ok, check.detail) == (False, "nothing came from the microphone")

    async def test_a_microphone_that_cannot_be_opened_is_the_error_of_the_backend(self) -> None:
        backend = FakeBackend()
        backend.refuses["open_input"] = "in use"

        check = await check_word_heard(
            a_config(listening={"mode": "wake_word"}), backend, an_engine(), seconds=1.0
        )

        assert (check.ok, check.detail) == (False, "in use")

    async def test_porcupine_says_that_it_heard_the_word_and_not_how_near_it_came(self) -> None:
        config = a_config(
            listening={"mode": "wake_word", "engine": "porcupine", "access_key": "a-key"}
        )
        backend = FakeBackend()

        check = await check_word_heard(config, backend, an_engine(), seconds=1.0)

        assert not check.ok
        assert "not how near it came" in check.detail
        assert "openwakeword" in (check.advice or "")
        assert backend.inputs == []

    async def test_an_engine_that_does_not_say_how_near_it_came_is_said_not_to(self) -> None:
        class Unscored:
            sample_rate, frame_length = 16_000, 512
            closed = False

            def process(self, frame: Any) -> bool:
                return False

            def reset(self) -> None:
                """Nothing to forget."""

            def close(self) -> None:
                self.closed = True

        unscored = Unscored()
        backend = FakeBackend()

        check = await check_word_heard(
            a_config(listening={"mode": "wake_word"}),
            backend,
            lambda listening: lambda: unscored,
            seconds=1.0,
        )

        assert not check.ok
        assert "does not say how near it came" in check.detail
        assert unscored.closed
        assert backend.inputs == []

    async def test_an_engine_that_cannot_be_had_is_said(self) -> None:
        def engine(listening: ListeningSettings) -> Callable[[], WakeWordEngine]:
            def make() -> WakeWordEngine:
                raise RuntimeError("the models are gone")

            return make

        check = await check_word_heard(
            a_config(listening={"mode": "wake_word"}), FakeBackend(), engine, seconds=1.0
        )

        assert (check.ok, check.detail) == (
            False,
            "the engine could not be had: the models are gone",
        )

    async def test_asked_to_listen_for_the_word_it_does_and_says_so_last(self) -> None:
        backend, lines = FakeBackend(), list[str]()
        edge = Edge()
        edge.connection.say(ready())
        config = a_config(listening={"mode": "wake_word"})
        checking = asyncio.create_task(
            examine(
                config,
                backend,
                word=True,
                opening=edge.open,
                engine=a_loud_sound_is_the_word(),
                say=lines.append,
            )
        )
        await until(lambda: backend.inputs)
        for at in range(70):  # five seconds are 62 frames
            backend.inputs[0].hear(FakeWakeWord.frame(6_000 if at == 20 else 0))

        checks = await asyncio.wait_for(checking, 3)

        assert [check.name for check in checks] == [*NAMES[:4], "wake word", "edge", "word"]
        assert checks[-1].ok
        assert "was heard" in checks[-1].detail
        assert lines == ["say 'hey jarvis' now: listening for 5 seconds"]

    async def test_it_listens_only_when_asked_and_when_there_is_something_to_listen_for(
        self, tmp_path: Path
    ) -> None:
        asked_for = a_config(listening={"mode": "wake_word", "models_dir": str(tmp_path)})
        unasked = FakeBackend()
        no_models = FakeBackend()
        no_microphone = FakeBackend()
        no_microphone.refuses["check_input"] = "no"

        not_asked = await examined(asked_for, backend=unasked)  # wake_word is only checked
        without_models = await examined(asked_for, backend=no_models, word=True, engine=engine_of)
        without_microphone = await examined(asked_for, backend=no_microphone, word=True)

        assert "word" not in by_name(not_asked)
        assert unasked.inputs == []
        assert "wake word" in by_name(without_models)
        assert "word" not in by_name(without_models)  # the models are not there to listen with
        assert no_models.inputs == []
        assert "word" not in by_name(without_microphone)  # nor is there a microphone to use
        assert no_microphone.inputs == []
