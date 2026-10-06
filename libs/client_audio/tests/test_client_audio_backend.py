"""The part of PortAudio that clients use, with a ``sounddevice`` that a test plays: what is listed,
what is checked, what is opened and what is said when it cannot be. Hardware is tried by hand
(``factotum-device doctor``)."""

import builtins
import sys
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from factotum.client_audio.backend import AudioDevice, AudioUnavailableError, SoundDeviceBackend


class Stream:
    """What ``sounddevice`` makes: it is started, and told things."""

    def __init__(self, **settings: Any) -> None:
        self.settings = settings
        self.calls: list[str] = []
        self.written: list[bytes] = []

    def start(self) -> None:
        self.calls.append("start")

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def stop(self) -> None:
        self.calls.append("stop")

    def abort(self) -> None:
        self.calls.append("abort")

    def close(self) -> None:
        self.calls.append("close")


class FakeSoundDevice:
    """The ``sounddevice`` module, as far as the backend uses it."""

    def __init__(self) -> None:
        self.default = SimpleNamespace(device=[0, 2])
        self.fails: dict[str, str] = {}
        self.checked: list[tuple[str, dict[str, Any]]] = []
        self.streams: list[Stream] = []

    def query_hostapis(self) -> list[dict[str, Any]]:
        self._maybe_fail("query_hostapis")
        return [{"name": "MME"}, {"name": "WASAPI"}]

    def query_devices(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "Microphone (USB)",
                "max_input_channels": 2,
                "max_output_channels": 0,
                "default_samplerate": 44_100.0,
                "hostapi": 0,
            },
            {
                "name": "Speakers",
                "max_input_channels": 0,
                "max_output_channels": 2,
                "default_samplerate": 48_000.0,
                "hostapi": 1,
            },
        ]

    def _maybe_fail(self, what: str) -> None:
        if what in self.fails:
            raise ValueError(self.fails[what])

    def check_input_settings(self, **settings: Any) -> None:
        self._maybe_fail("check_input_settings")
        self.checked.append(("input", settings))

    def check_output_settings(self, **settings: Any) -> None:
        self._maybe_fail("check_output_settings")
        self.checked.append(("output", settings))

    def InputStream(self, **settings: Any) -> Stream:  # noqa: N802 - the name of the class it stands for
        self._maybe_fail("InputStream")
        stream = Stream(**settings)
        self.streams.append(stream)
        return stream

    def RawOutputStream(self, **settings: Any) -> Stream:  # noqa: N802
        self._maybe_fail("RawOutputStream")
        stream = Stream(**settings)
        self.streams.append(stream)
        return stream


def backend_with(
    module: FakeSoundDevice | None = None,
) -> tuple[SoundDeviceBackend, FakeSoundDevice]:
    module = module or FakeSoundDevice()
    return SoundDeviceBackend(module), module


class TestListingTheDevices:
    def test_each_device_says_what_it_is_and_what_it_hears_or_plays(self) -> None:
        backend, _ = backend_with()

        assert backend.devices() == [
            AudioDevice(0, "Microphone (USB)", 2, 0, 44_100.0, "MME"),
            AudioDevice(1, "Speakers", 0, 2, 48_000.0, "WASAPI"),
        ]

    def test_the_defaults_are_the_ones_the_system_says(self) -> None:
        backend, _ = backend_with()

        assert (backend.default_input(), backend.default_output()) == (0, 2)

    @pytest.mark.parametrize("nothing", [-1, None])
    def test_a_system_with_no_default_has_none(self, nothing: int | None) -> None:
        backend, module = backend_with()
        module.default.device = [nothing, nothing]

        assert (backend.default_input(), backend.default_output()) == (None, None)

    def test_what_portaudio_fails_with_is_one_error(self) -> None:
        backend, module = backend_with()
        module.fails["query_hostapis"] = "the host API did not answer"

        with pytest.raises(AudioUnavailableError, match="could not be listed"):
            backend.devices()


class TestCheckingWithoutOpening:
    def test_one_channel_of_16_bit_samples_at_the_rate_is_what_is_asked_of_a_microphone(
        self,
    ) -> None:
        backend, module = backend_with()

        backend.check_input("USB", 16_000)

        assert module.checked == [
            ("input", {"device": "USB", "channels": 1, "dtype": "int16", "samplerate": 16_000})
        ]
        assert module.streams == []  # nothing was opened

    def test_and_of_a_speaker(self) -> None:
        backend, module = backend_with()

        backend.check_output(None, 22_050)

        assert module.checked == [
            ("output", {"device": None, "channels": 1, "dtype": "int16", "samplerate": 22_050})
        ]

    @pytest.mark.parametrize("which", ["input", "output"])
    def test_a_device_that_cannot_is_an_error_that_says_why(self, which: str) -> None:
        backend, module = backend_with()
        module.fails[f"check_{which}_settings"] = "Invalid sample rate"

        with pytest.raises(AudioUnavailableError, match="Invalid sample rate"):
            getattr(backend, f"check_{which}")(None, 16_000)


class TestOpening:
    def test_a_microphone_hands_over_the_first_channel_of_each_block(self) -> None:
        backend, module = backend_with()
        heard: list[Any] = []

        backend.open_input("USB", 16_000, 512, heard.append)
        [stream] = module.streams
        stream.settings["callback"](
            np.array([[1, 9], [2, 9], [3, 9]], dtype=np.int16), 3, None, None
        )

        assert stream.calls == ["start"]
        assert {k: v for k, v in stream.settings.items() if k != "callback"} == {
            "device": "USB",
            "samplerate": 16_000,
            "channels": 1,
            "dtype": "int16",
            "blocksize": 512,
        }
        assert heard[0].tolist() == [1, 2, 3]

    def test_what_a_microphone_hands_over_is_a_copy_that_the_hardware_does_not_reuse(self) -> None:
        backend, module = backend_with()
        heard: list[Any] = []
        backend.open_input(None, 16_000, 4, heard.append)
        buffer = np.array([[1], [2]], dtype=np.int16)

        module.streams[0].settings["callback"](buffer, 2, None, None)
        buffer[:] = 7  # the hardware fills the same buffer again

        assert heard[0].tolist() == [1, 2]

    def test_a_speaker_is_one_channel_of_16_bit_samples_and_is_started(self) -> None:
        backend, module = backend_with()

        stream = backend.open_output(None, 22_050)

        assert stream is module.streams[0]
        assert module.streams[0].settings == {
            "device": None,
            "samplerate": 22_050,
            "channels": 1,
            "dtype": "int16",
        }
        assert module.streams[0].calls == ["start"]

    @pytest.mark.parametrize(
        ("what", "opens"),
        [("InputStream", "input"), ("RawOutputStream", "output")],
    )
    def test_a_device_that_will_not_open_is_an_error_that_says_why(
        self, what: str, opens: str
    ) -> None:
        backend, module = backend_with()
        module.fails[what] = "Unanticipated host error"

        openers: dict[str, Callable[[], object]] = {
            "input": lambda: backend.open_input(None, 16_000, 512, lambda block: None),
            "output": lambda: backend.open_output(None, 22_050),
        }

        with pytest.raises(AudioUnavailableError, match="Unanticipated host error"):
            openers[opens]()


class TestWithoutPortAudio:
    def test_a_machine_that_has_the_library_and_not_portaudio_says_so(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_import = builtins.__import__

        def refuse(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "sounddevice":
                raise OSError("PortAudio library not found")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", refuse)

        with pytest.raises(AudioUnavailableError, match="PortAudio could not be loaded"):
            SoundDeviceBackend().devices()

    def test_the_library_is_not_imported_until_it_is_needed(self) -> None:
        backend = SoundDeviceBackend()

        assert backend._module is None

    def test_when_it_is_needed_it_is_the_library_that_is_imported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        library = FakeSoundDevice()
        monkeypatch.setitem(sys.modules, "sounddevice", library)
        backend = SoundDeviceBackend()

        assert backend.default_input() == 0
        assert backend._module is library
