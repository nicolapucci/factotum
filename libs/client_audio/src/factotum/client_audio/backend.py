"""What the sound hardware is to a client: devices that can be listed and checked, a stream that
hands over what a microphone hears a block at a time, and a stream that takes what is to be played.

``AudioBackend`` is the small part of PortAudio that clients use, and ``SoundDeviceBackend`` is it,
through ``sounddevice``. Everything else of this library is written against the interface, so that
what decides things (where a person's speech ends, which sentence is playing, what a volume does to
the samples) is tried with a backend that a test plays, and what needs hardware is as little as it
can be: the few lines below, which are tried by hand (``factotum-device doctor`` does).

``sounddevice`` is imported when a backend first needs it, so that a client that never makes a sound
(or a machine that has no PortAudio) never needs it installed.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

DeviceChoice = int | str | None
"""Which device: its index, a part of its name, or None for the default of the system."""


class AudioUnavailableError(OSError):
    """The sound hardware cannot do what was asked: there is no such device, it does not take that
    rate, something else is using it, or the system does not let this program hear (on Windows, the
    privacy setting of the microphone)."""


@dataclass(frozen=True, slots=True)
class AudioDevice:
    """A device as the system lists it."""

    index: int
    name: str
    inputs: int
    """How many channels it can hear (0: it is not a microphone)."""

    outputs: int
    """How many channels it can play (0: it is not a speaker)."""

    sample_rate: float
    """The rate the system prefers for it."""

    api: str
    """The host API it is listed by (MME, WASAPI, ALSA...): the same hardware is listed by each."""


class InputStream(Protocol):
    """What is hearing: closing it stops it."""

    def close(self) -> None: ...


class OutputStream(Protocol):
    """What is playing. ``write`` waits until the device has room for the samples, which is what
    keeps a client from running ahead of the sound; ``stop`` waits for what was written to be
    played; ``abort`` does not wait for anything, and lets a ``write`` that is waiting go."""

    def write(self, pcm: bytes) -> None: ...

    def stop(self) -> None: ...

    def abort(self) -> None: ...

    def close(self) -> None: ...


class AudioBackend(Protocol):
    def devices(self) -> list[AudioDevice]: ...

    def default_input(self) -> int | None: ...

    def default_output(self) -> int | None: ...

    def check_input(self, device: DeviceChoice, sample_rate: int) -> None:
        """Raises ``AudioUnavailableError`` unless the device can hear one channel of 16-bit samples
        at this rate. Nothing is opened, and nothing is heard."""
        ...

    def check_output(self, device: DeviceChoice, sample_rate: int) -> None:
        """The same for playing. Nothing is opened, and nothing is played."""
        ...

    def open_input(
        self,
        device: DeviceChoice,
        sample_rate: int,
        block_size: int,
        on_block: Callable[[Any], None],
    ) -> InputStream:
        """Start hearing. ``on_block`` is given each block of ``block_size`` samples (a numpy array
        of 16-bit samples) on a thread of the hardware's own: it must be quick, and it must not
        wait for anything."""
        ...

    def open_output(self, device: DeviceChoice, sample_rate: int) -> OutputStream:
        """Start playing one channel of 16-bit samples at this rate."""
        ...


class SoundDeviceBackend:
    """PortAudio, through ``sounddevice``. ``module`` is for a test to put its own in the place of
    the library; nothing is imported until it is needed."""

    def __init__(self, module: Any | None = None) -> None:
        self._module = module

    def _sd(self) -> Any:
        if self._module is None:
            try:
                import sounddevice
            except OSError as error:  # the library is there and PortAudio, which it needs, is not
                raise AudioUnavailableError(f"PortAudio could not be loaded: {error}") from error
            self._module = sounddevice
        return self._module

    def devices(self) -> list[AudioDevice]:
        sd = self._sd()
        try:
            apis = sd.query_hostapis()
            return [
                AudioDevice(
                    index=index,
                    name=str(found["name"]),
                    inputs=int(found["max_input_channels"]),
                    outputs=int(found["max_output_channels"]),
                    sample_rate=float(found["default_samplerate"]),
                    api=str(apis[found["hostapi"]]["name"]),
                )
                for index, found in enumerate(sd.query_devices())
            ]
        except Exception as error:
            raise AudioUnavailableError(f"the devices could not be listed: {error}") from error

    def default_input(self) -> int | None:
        return self._default(0)

    def default_output(self) -> int | None:
        return self._default(1)

    def _default(self, which: int) -> int | None:
        chosen = self._sd().default.device[which]
        return int(chosen) if chosen is not None and int(chosen) >= 0 else None

    def check_input(self, device: DeviceChoice, sample_rate: int) -> None:
        try:
            self._sd().check_input_settings(
                device=device, channels=1, dtype="int16", samplerate=sample_rate
            )
        except Exception as error:
            raise AudioUnavailableError(str(error)) from error

    def check_output(self, device: DeviceChoice, sample_rate: int) -> None:
        try:
            self._sd().check_output_settings(
                device=device, channels=1, dtype="int16", samplerate=sample_rate
            )
        except Exception as error:
            raise AudioUnavailableError(str(error)) from error

    def open_input(
        self,
        device: DeviceChoice,
        sample_rate: int,
        block_size: int,
        on_block: Callable[[Any], None],
    ) -> InputStream:
        def callback(indata: Any, frames: int, time: Any, status: Any) -> None:
            on_block(indata[:, 0].copy())

        try:
            stream = self._sd().InputStream(
                device=device,
                samplerate=sample_rate,
                channels=1,
                dtype="int16",
                blocksize=block_size,
                callback=callback,
            )
            stream.start()
        except Exception as error:
            raise AudioUnavailableError(str(error)) from error
        return stream  # type: ignore[no-any-return]

    def open_output(self, device: DeviceChoice, sample_rate: int) -> OutputStream:
        try:
            stream = self._sd().RawOutputStream(
                device=device, samplerate=sample_rate, channels=1, dtype="int16"
            )
            stream.start()
        except Exception as error:
            raise AudioUnavailableError(str(error)) from error
        return stream  # type: ignore[no-any-return]
