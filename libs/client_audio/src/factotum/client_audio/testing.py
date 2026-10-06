"""A sound card that a test plays: what is played is kept, what is heard is what the test says,
and a speaker can be made to take its time, which is when a person speaks over it.

``FakeBackend`` implements ``AudioBackend``, so that what a test runs is the library and the client
above it, with nothing replaced but the hardware. ``FakeWakeWord`` is the engine of openWakeWord on
models that are made up, which answer with what they were given so that what flows from one to the
next can be read.
"""

import asyncio
import threading
from collections.abc import Callable, Sequence
from typing import Any

import numpy as np

from factotum.client_audio.backend import (
    AudioDevice,
    AudioUnavailableError,
    DeviceChoice,
)
from factotum.client_audio.wakeword import OpenWakeWordEngine

MICROPHONE = AudioDevice(0, "Fake microphone", 1, 0, 16_000.0, "Fake API")
SPEAKER = AudioDevice(1, "Fake speaker", 0, 2, 44_100.0, "Fake API")


class FakeInput:
    """A microphone that is hearing, and whose blocks are what a test says."""

    def __init__(
        self,
        device: DeviceChoice,
        sample_rate: int,
        block_size: int,
        on_block: Callable[[Any], None],
    ) -> None:
        self.device = device
        self.sample_rate = sample_rate
        self.block_size = block_size
        self.closed = False
        self._on_block = on_block

    def hear(self, block: Any) -> None:
        """A block of what the microphone hears, handed over as the hardware does: from a thread
        that is not the loop's."""
        if not self.closed:
            self._on_block(block)

    def close(self) -> None:
        self.closed = True


class FakeOutput:
    """A speaker that is playing: what it was given is kept, and with ``gate`` a ``write`` waits
    until the gate is opened or the speaker is told to stop."""

    def __init__(
        self, device: DeviceChoice, sample_rate: int, gate: threading.Event | None
    ) -> None:
        self.device = device
        self.sample_rate = sample_rate
        self.calls: list[str] = []
        """What it was told, in order: ``write``, ``stop``, ``abort``, ``close``."""

        self.writes: list[bytes] = []
        """What it took, a write at a time."""

        self.waiting = threading.Event()
        """Set when a ``write`` is waiting for the gate."""

        self._gate = gate
        self._aborted = threading.Event()

    @property
    def sound(self) -> bytes:
        return b"".join(self.writes)

    def write(self, pcm: bytes) -> None:
        self.calls.append("write")
        if self._gate is not None:
            self.waiting.set()
            while not self._gate.is_set():
                if self._aborted.wait(0.005):
                    raise AudioUnavailableError("the speaker was told to stop")
        self.writes.append(pcm)

    def stop(self) -> None:
        self.calls.append("stop")

    def abort(self) -> None:
        self.calls.append("abort")
        self._aborted.set()

    def close(self) -> None:
        self.calls.append("close")


class FakeBackend:
    """The sound hardware of a machine, as a test has it: the devices it lists, the streams that
    were opened and what became of them, and what it refuses."""

    def __init__(
        self,
        devices: list[AudioDevice] | None = None,
        *,
        default_input: int | None = 0,
        default_output: int | None = 1,
    ) -> None:
        self.listed = [MICROPHONE, SPEAKER] if devices is None else devices
        self._default_input = default_input
        self._default_output = default_output
        self.inputs: list[FakeInput] = []
        self.outputs: list[FakeOutput] = []
        self.checked: list[tuple[str, DeviceChoice, int]] = []
        """What it was asked whether it could do: ``("input" | "output", device, rate)``."""

        self.refuses: dict[str, str] = {}
        """What it refuses and what it says: ``check_input``, ``check_output``, ``open_input``,
        ``open_output``."""

        self.gate: threading.Event | None = None
        """When set, what is opened to play takes its time: a ``write`` waits for it."""

    def _refuse(self, what: str) -> None:
        if what in self.refuses:
            raise AudioUnavailableError(self.refuses[what])

    def devices(self) -> list[AudioDevice]:
        self._refuse("devices")
        return list(self.listed)

    def default_input(self) -> int | None:
        return self._default_input

    def default_output(self) -> int | None:
        return self._default_output

    def check_input(self, device: DeviceChoice, sample_rate: int) -> None:
        self.checked.append(("input", device, sample_rate))
        self._refuse("check_input")

    def check_output(self, device: DeviceChoice, sample_rate: int) -> None:
        self.checked.append(("output", device, sample_rate))
        self._refuse("check_output")

    def open_input(
        self,
        device: DeviceChoice,
        sample_rate: int,
        block_size: int,
        on_block: Callable[[Any], None],
    ) -> FakeInput:
        self._refuse("open_input")
        stream = FakeInput(device, sample_rate, block_size, on_block)
        self.inputs.append(stream)
        return stream

    def open_output(self, device: DeviceChoice, sample_rate: int) -> FakeOutput:
        self._refuse("open_output")
        stream = FakeOutput(device, sample_rate, self.gate)
        self.outputs.append(stream)
        return stream

    @property
    def hearing(self) -> FakeInput:
        """The microphone that is open now (the last that was opened, and is not closed)."""
        open_ones = [stream for stream in self.inputs if not stream.closed]
        if not open_ones:
            raise AssertionError("no microphone is open")
        return open_ones[-1]


class KeyPresses:
    """A key that a test presses: ``press`` asks to speak, ``leave`` is the person going, and
    ``again`` is the key pressed once more while they speak (they have said they are done)."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[bool] = asyncio.Queue()
        self.again = False

    def press(self) -> None:
        self._queue.put_nowait(True)

    def leave(self) -> None:
        self._queue.put_nowait(False)

    def __aiter__(self) -> "KeyPresses":
        return self

    async def __anext__(self) -> None:
        if not await self._queue.get():
            raise StopAsyncIteration

    def pending(self) -> bool:
        pressed, self.again = self.again, False
        return pressed


# ---- the engine of openWakeWord, on models that are made up ------------------------------------


class FakeNode:
    """What ``get_inputs`` has in it: the name of an input and its shape."""

    def __init__(self, name: str, shape: Sequence[Any]) -> None:
        self.name = name
        self.shape = list(shape)


class FakeSession:
    """A session that answers with what ``answer`` makes of what it is asked, and remembers what it
    was asked."""

    def __init__(
        self, name: str, shape: Sequence[Any], answer: Callable[[np.ndarray], np.ndarray]
    ) -> None:
        self._node = FakeNode(name, shape)
        self._answer = answer
        self.asked: list[np.ndarray] = []

    def get_inputs(self) -> list[FakeNode]:
        return [self._node]

    def run(self, output_names: Any, input_feed: Any) -> list[np.ndarray]:
        [(name, value)] = input_feed.items()
        if output_names is not None or name != self._node.name:
            raise AssertionError("a model is asked for all its outputs, by the name of its input")
        if value.dtype != np.float32:  # onnxruntime refuses what is not the type of the input
            raise AssertionError(f"a model takes 32-bit floats, and was given {value.dtype}")
        self.asked.append(value)
        return [self._answer(value)]


def rows_of_spectrogram(samples: int) -> int:
    """How many rows of spectrogram the real model makes of so many samples (measured)."""
    return (samples - 480) // 160


def fake_spectrogram(value: np.ndarray) -> np.ndarray:
    """A row of 32 numbers for each ten milliseconds, every one the mean of the sound."""
    rows = rows_of_spectrogram(value.shape[1])
    return np.full((1, 1, rows, 32), value.mean(), dtype=np.float32)


def fake_embedding(value: np.ndarray) -> np.ndarray:
    """An embedding of 96 numbers for each window, every one the mean of its window."""
    means = value.mean(axis=(1, 2, 3)).astype(np.float32)
    return np.broadcast_to(means[:, None, None, None], (len(value), 1, 1, 96)).copy()


class FakeWakeWord:
    """The three models of the engine, made up, and the engine on them. ``score`` is what the model
    of the word says (a number, or a function of the embeddings that it is given)."""

    def __init__(
        self,
        score: float | Callable[[np.ndarray], float] = 1.0,
        *,
        frames: Any = 16,
        **engine: Any,
    ) -> None:
        judge: Callable[[np.ndarray], float] = score if callable(score) else (lambda value: score)
        self.melspectrogram = FakeSession("input", ["batch", "samples"], fake_spectrogram)
        self.embedding = FakeSession("input_1", ["n", 76, 32, 1], fake_embedding)
        self.word = FakeSession(
            "x.1", [1, frames, 96], lambda value: np.array([[judge(value)]], dtype=np.float32)
        )
        self.engine = OpenWakeWordEngine(self.melspectrogram, self.embedding, self.word, **engine)

    @staticmethod
    def frame(fill: int = 100) -> np.ndarray:
        """A frame of 80 ms."""
        return np.full(1_280, fill, dtype=np.int16)
