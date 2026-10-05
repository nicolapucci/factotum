"""What the engine of Whisper decides, with a fake model in place of ``WhisperModel``.

The engine reads a recording with the decoder of faster-whisper (the real one: PyAV is installed),
picks the language the model is to listen in, and joins what the model heard. Which words a real
model finds in a real voice is for the end-to-end tests with real speech.
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from factotum.model_gateway.providers.whisper_local import (
    SAMPLE_RATE,
    FasterWhisperEngine,
    choose_language,
)
from factotum.model_gateway.testing import a_tone_wav


@dataclass(frozen=True)
class Segment:
    text: str


class FakeWhisperModel:
    """What the engine asks of ``WhisperModel``: to listen to samples and say what was heard, and to
    say how likely each language is."""

    def __init__(
        self,
        said: Sequence[str] = (" ciao ", "a tutti. "),
        likelihoods: Sequence[tuple[str, float]] = (("it", 0.9), ("en", 0.1)),
    ) -> None:
        self._said = said
        self._likelihoods = likelihoods
        self.heard: list[tuple[int, str | None]] = []
        """The number of samples and the language of each recording that was transcribed."""
        self.options: list[dict[str, Any]] = []
        """What else each recording was transcribed with."""
        self.detections = 0

    def transcribe(
        self, audio: Any, language: str | None = None, **options: Any
    ) -> tuple[Iterator[Segment], None]:
        self.heard.append((len(audio), language))
        self.options.append(options)
        return (Segment(text) for text in self._said), None

    def detect_language(self, audio: Any) -> tuple[str, float, list[tuple[str, float]]]:
        self.detections += 1
        best, likelihood = self._likelihoods[0]
        return best, likelihood, list(self._likelihoods)


def engine_of(model: FakeWhisperModel) -> tuple[FasterWhisperEngine, list[int]]:
    loads: list[int] = []

    def load() -> FakeWhisperModel:
        loads.append(1)
        return model

    return FasterWhisperEngine("base", device="cpu", cache_dir=None, load_model=load), loads


class TestTheEngineOfWhisper:
    def test_what_the_model_heard_is_joined_and_trimmed(self) -> None:
        engine, _ = engine_of(FakeWhisperModel())

        assert engine.transcribe_wav(a_tone_wav(16000), None) == "ciao a tutti."

    def test_a_recording_reaches_the_model_decoded_to_16_khz_whatever_it_was_made_at(self) -> None:
        model = FakeWhisperModel()
        engine, _ = engine_of(model)

        engine.transcribe_wav(a_tone_wav(22050, seconds=1.0), None)

        [(samples, _language)] = model.heard
        assert samples == pytest.approx(SAMPLE_RATE, abs=320)

    def test_what_is_not_speech_is_left_out_before_the_model_listens(self) -> None:
        # Told to hear Italian, the real Whisper writes "Sottotitoli e revisione a cura di QTSS" for
        # a second of silence: found by the end-to-end test of a silence, with real speech.
        model = FakeWhisperModel()
        engine, _ = engine_of(model)

        engine.transcribe_wav(a_tone_wav(16000), ("it",))

        assert model.options == [{"vad_filter": True}]

    def test_the_model_is_loaded_once_and_not_before_it_is_asked(self) -> None:
        engine, loads = engine_of(FakeWhisperModel())
        assert loads == []

        engine.transcribe_wav(a_tone_wav(16000), None)
        engine.transcribe_wav(a_tone_wav(16000), None)

        assert loads == [1]

    def test_with_no_language_the_model_guesses_among_all_of_its_own(self) -> None:
        model = FakeWhisperModel()
        engine, _ = engine_of(model)

        engine.transcribe_wav(a_tone_wav(16000), None)

        assert [language for _, language in model.heard] == [None]
        assert model.detections == 0

    def test_a_language_that_is_the_only_one_is_forced_without_guessing(self) -> None:
        model = FakeWhisperModel()
        engine, _ = engine_of(model)

        engine.transcribe_wav(a_tone_wav(16000), ("it",))

        assert [language for _, language in model.heard] == ["it"]
        assert model.detections == 0

    def test_among_several_the_likeliest_of_them_is_used_and_not_the_likeliest_of_all(self) -> None:
        # Arabic is what it found likeliest in "apri il garage", and nobody speaks it here.
        model = FakeWhisperModel(likelihoods=(("ar", 0.6), ("en", 0.15), ("it", 0.2)))
        engine, _ = engine_of(model)

        engine.transcribe_wav(a_tone_wav(16000), ("it", "en"))

        assert [language for _, language in model.heard] == ["it"]
        assert model.detections == 1

    def test_the_first_language_is_used_when_the_model_found_none_of_them(self) -> None:
        model = FakeWhisperModel(likelihoods=(("ar", 0.6), ("fa", 0.3)))
        engine, _ = engine_of(model)

        engine.transcribe_wav(a_tone_wav(16000), ("it", "en"))

        assert [language for _, language in model.heard] == ["it"]


CODES = st.sampled_from(["it", "en", "fr", "de", "es", "pt", "ar", "pl"])


class TestChoosingALanguage:
    @given(
        allowed=st.lists(CODES, min_size=1, unique=True).map(tuple),
        likelihoods=st.lists(st.tuples(CODES, st.floats(min_value=0, max_value=1))),
    )
    def test_it_is_always_one_the_person_may_speak_and_the_likeliest_of_them(
        self, allowed: tuple[str, ...], likelihoods: list[tuple[str, float]]
    ) -> None:
        chosen = choose_language(allowed, likelihoods)
        scores = dict(likelihoods)

        assert chosen in allowed
        assert scores.get(chosen, 0.0) == max(scores.get(code, 0.0) for code in allowed)

    def test_a_tie_goes_to_the_one_that_is_listed_first(self) -> None:
        assert choose_language(("it", "en"), [("en", 0.4), ("it", 0.4)]) == "it"
        assert choose_language(("en", "it"), [("en", 0.4), ("it", 0.4)]) == "en"
