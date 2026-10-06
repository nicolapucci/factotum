"""What the doctor says is wrong, with a sound card and an edge that a test plays: each check by
itself, what it says when it fails and what to do about it, and that it makes no sound and hears
nothing unless it was asked to."""

import asyncio

import numpy as np
import pytest

from factotum.client_audio.backend import AudioDevice
from factotum.client_audio.testing import MICROPHONE, SPEAKER, FakeBackend
from factotum.client_core.testing import MemoryConnection
from factotum.contracts.capabilities import Capability
from factotum.contracts.edge import ErrorFrame, Feature
from factotum.device import doctor
from factotum.device.config import Config
from factotum.device.doctor import (
    SPEAKER_RATE,
    Check,
    check_listening,
    check_playing,
    examine,
    report_of,
    tone,
)
from factotum.device.testing import Edge, a_config, ready, until

NAMES = ["settings", "sound devices", "microphone", "speaker", "edge"]


async def examined(
    config: Config | None = None,
    *,
    edge: Edge | None = None,
    backend: FakeBackend | None = None,
    listen: bool = False,
    play: bool = False,
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
        opening=edge.open,
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
        config = a_config(listening={"mode": "wake_word", "access_key": "a-key"})

        checks = await examined(config)

        assert "wake_word ('jarvis')" in by_name(checks)["settings"].detail
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
