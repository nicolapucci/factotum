"""The wake word with the real models (ADR 0027): the three models of openWakeWord on onnxruntime,
hearing the Italian voice of Piper's say a word and what is not one, and a device that is woken by
it with the real Whisper listening to what follows.

What is real is what these tests are about: that the runtime of ``factotum.client_audio.wakeword``
works with the files that its author published (the unit tests have models that are made up), that a
word is told from what is not one, and that what a device sends after the word is what was said
after it. What is made up is the sound card, which a test plays.

That the runtime gives the scores of the package that it was taken from, to the last digit, was
measured when it was written (ADR 0027 says how and on what) and is not run here: the package is
not a dependency of this project.

They are live tests, run by a person with the models of this machine and never with a download:
``FACTOTUM_LIVE_TESTS=1 uv run pytest e2e/tests/test_e2e_wakeword.py -m live -s``. The models of
openWakeWord are not in this repository (they are their author's, CC BY-NC-SA 4.0): the folder
that a device looks in has them once ``factotum-device wake-word download`` was run for each word,
or ``FACTOTUM_LIVE_WAKEWORD_MODELS`` says another. A synthetic voice is not a person, and what it
scores says whether a word is out of reach for an Italian accent and not what a person's voice will
do: ``factotum-device doctor --word`` is how a person finds out.
"""

import asyncio
import io
import os
import wave
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest

from factotum.client_audio import (
    WAKE_WORDS,
    Engine,
    OpenWakeWordEngine,
    WakeWordModels,
    wake_word_engine,
)
from factotum.client_audio.testing import FakeBackend
from factotum.client_core import VoiceInput
from factotum.client_core.audio import AudioStream
from factotum.client_core.testing import RecordingRenderer
from factotum.contracts.audio import AudioPayload, wav_of
from factotum.device.app import build_hardware, engine_of
from factotum.device.doctor import Check, check_word_heard
from factotum.device.testing import a_config, until
from factotum.e2e.speech import Person, RealSpeech, hears

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("FACTOTUM_LIVE_TESTS") != "1",
        reason="live tests are opt-in: set FACTOTUM_LIVE_TESTS=1",
    ),
]

FRAME = 1_280
RATE = 16_000
MODELS_VARIABLE = "FACTOTUM_LIVE_WAKEWORD_MODELS"

NOT_A_WORD = {
    "a command": "Accendi la luce del salotto",
    "a conversation": "Domani andiamo al mare con i bambini se il tempo regge",
    "a name that is almost one": "Giarvis",
}
"""What is said at a device all day and is none of the words."""


@pytest.fixture(autouse=True)
def _how_the_house_is_run() -> None:
    """The house is not run here (this takes the place of the fixture of the conftest, which runs a
    whole one three ways for each test): the word is a device's, and what is tried is the engine and
    the input that it is behind."""


@pytest.fixture(scope="module")
def models() -> WakeWordModels:
    """The models of every word, in the folder that a device looks in (or the one that is said)."""
    folder = os.environ.get(MODELS_VARIABLE)
    found = WakeWordModels(Path(folder) if folder else None)
    gone = [word for word in sorted(WAKE_WORDS) if found.missing(word)]
    if gone:
        pytest.skip(
            f"the models of {', '.join(gone)} are not in {found.directory}, and none is "
            f"downloaded here: {found.download_command(gone[0])}"
        )
    return found


def samples_of(clip: AudioPayload) -> np.ndarray:
    with wave.open(io.BytesIO(clip.data), "rb") as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (RATE, 1, 2)
        return np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)


def quiet(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * RATE), dtype=np.int16)


def frames_of(*sounds: np.ndarray) -> list[np.ndarray]:
    """What a microphone hands over for these sounds, one after the other: frames of 80 ms."""
    sound = np.concatenate(sounds)
    padded = np.concatenate([sound, np.zeros(-len(sound) % FRAME, dtype=np.int16)])
    return [padded[start : start + FRAME] for start in range(0, len(padded), FRAME)]


def engine_for(models: WakeWordModels, word: str) -> OpenWakeWordEngine:
    made = wake_word_engine(Engine.OPENWAKEWORD, word, models=models)()
    assert isinstance(made, OpenWakeWordEngine)
    return made


class TestTheRealModels:
    def test_a_word_that_is_said_is_heard_by_the_model_of_that_word(
        self, person: Person, models: WakeWordModels
    ) -> None:
        engine = engine_for(models, "alexa")
        frames = frames_of(quiet(1.0), samples_of(person.saying("Alexa")), quiet(1.0))

        assert any(engine.process(frame) for frame in frames)

    def test_the_first_scores_are_zero_and_every_score_is_a_score(
        self, person: Person, models: WakeWordModels
    ) -> None:
        engine = engine_for(models, "alexa")
        frames = frames_of(quiet(1.0), samples_of(person.saying("Alexa")))

        scores = [engine.score(frame) for frame in frames]

        assert scores[:5] == [0.0] * 5
        assert all(0.0 <= score <= 1.0 for score in scores)
        assert max(scores) > 0.9  # an English word, said by a voice that has an accent

    @pytest.mark.parametrize("other", ["hey_jarvis", "hey_mycroft", "hey_rhasspy"])
    def test_it_is_not_heard_by_the_model_of_another_word(
        self, person: Person, models: WakeWordModels, other: str
    ) -> None:
        engine = engine_for(models, other)
        frames = frames_of(quiet(1.0), samples_of(person.saying("Alexa")), quiet(1.0))

        assert not any(engine.process(frame) for frame in frames)

    @pytest.mark.parametrize("word", sorted(WAKE_WORDS))
    @pytest.mark.parametrize("said", NOT_A_WORD.values(), ids=NOT_A_WORD.keys())
    def test_what_is_not_a_word_is_none_of_them(
        self, person: Person, models: WakeWordModels, word: str, said: str
    ) -> None:
        engine = engine_for(models, word)
        frames = frames_of(quiet(1.0), samples_of(person.saying(said)), quiet(1.0))

        assert not any(engine.process(frame) for frame in frames)

    def test_it_hears_the_word_again_after_it_was_made_to_forget_what_it_heard(
        self, person: Person, models: WakeWordModels
    ) -> None:
        engine = engine_for(models, "alexa")
        frames = frames_of(quiet(1.0), samples_of(person.saying("Alexa")), quiet(1.0))
        assert any(engine.process(frame) for frame in frames)

        engine.reset()

        assert any(engine.process(frame) for frame in frames)


class TestADeviceThatIsWokenByIt:
    def a_device_that_waits_for(self, models: WakeWordModels, backend: FakeBackend) -> VoiceInput:
        config = a_config(
            listening={
                "mode": "wake_word",
                "wake_word": "alexa",
                "silence_seconds": 1.0,
                "models_dir": str(models.directory),
            }
        )
        hardware = build_hardware(config, backend, RecordingRenderer(), engine=engine_of)
        assert hardware.voice_input is not None
        return hardware.voice_input

    async def test_what_follows_the_word_is_what_is_sent_and_it_is_heard_again_after(
        self, speech: RealSpeech, person: Person, models: WakeWordModels
    ) -> None:
        backend = FakeBackend()
        heard = cast("AsyncGenerator[Any]", self.a_device_that_waits_for(models, backend).listen())
        word = samples_of(person.saying("Alexa"))
        question = samples_of(person.saying("Che ore sono?"))
        try:
            for _ in range(2):  # the second time is a device that has forgotten what it heard
                waiting = asyncio.ensure_future(anext(heard))
                await until(lambda: backend.inputs and not backend.inputs[-1].closed, 10)
                for frame in frames_of(quiet(1.0), word, question, quiet(2.5)):
                    backend.hearing.hear(frame)
                    await asyncio.sleep(0)
                stream = await asyncio.wait_for(waiting, 30)
                assert isinstance(stream, AudioStream)
                pcm = b"".join([chunk async for chunk in stream.chunks])

                said = hears(speech, wav_of(pcm, RATE)).lower()
                assert len(pcm) / 2 / RATE >= len(question) / RATE
                assert "ore" in said
                assert "alexa" not in said
        finally:
            await heard.aclose()

    async def test_a_microphone_is_opened_at_the_rate_and_the_frame_of_the_models(
        self, models: WakeWordModels
    ) -> None:
        backend = FakeBackend()
        heard = cast("AsyncGenerator[Any]", self.a_device_that_waits_for(models, backend).listen())
        waiting = asyncio.ensure_future(anext(heard))
        try:
            await until(lambda: backend.inputs, 10)

            [microphone] = backend.inputs
            assert (microphone.sample_rate, microphone.block_size) == (RATE, FRAME)
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
            await heard.aclose()


class TestTheDoctorListeningForTheWord:
    async def listened(
        self, models: WakeWordModels, *sounds: np.ndarray, word: str = "alexa"
    ) -> Check:
        backend = FakeBackend()
        config = a_config(
            listening={"mode": "wake_word", "wake_word": word, "models_dir": str(models.directory)}
        )
        checking = asyncio.create_task(check_word_heard(config, backend, engine_of))
        await until(lambda: backend.inputs, 10)
        for frame in frames_of(*sounds):
            backend.inputs[0].hear(frame)
        return await asyncio.wait_for(checking, 30)

    async def test_a_word_that_is_said_is_heard_and_says_how_much(
        self, person: Person, models: WakeWordModels
    ) -> None:
        check = await self.listened(
            models, quiet(1.0), samples_of(person.saying("Alexa")), quiet(4.0)
        )

        assert check.ok
        assert "'alexa' was heard" in check.detail

    async def test_what_is_not_the_word_is_not_heard_and_says_that_it_did_not_come_near(
        self, person: Person, models: WakeWordModels
    ) -> None:
        said = samples_of(person.saying(NOT_A_WORD["a conversation"]))

        check = await self.listened(models, quiet(1.0), said, quiet(4.0))

        assert not check.ok
        assert "did not come near" in (check.advice or "")
