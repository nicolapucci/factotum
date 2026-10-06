"""The two local speech adapters: faster-whisper (speech-to-text) and Piper (text-to-speech).

Neither adapter talks to a real model here: each takes an ``engine_factory`` that hands out a
fake standing in for ``faster_whisper.WhisperModel`` or ``piper.PiperVoice``, so these tests never
load a model or touch a GPU. What the engine of Whisper decides is tested in
``test_whisper_engine.py`` with a fake model; the models themselves are exercised only by the live
tests and by the end-to-end ones with real speech, by hand, against the real libraries.
"""

import threading
import wave
from collections.abc import Iterator, Sequence
from io import BytesIO

import pytest

from factotum.contracts.audio import AudioPayload
from factotum.contracts.errors import ProviderUnavailableError, RoleNotSupportedError
from factotum.contracts.models import BindingOptions, ProviderKind
from factotum.contracts.testing import make_model_binding, make_tenant_id
from factotum.model_gateway.providers.base import ResolvedBinding, SpeechPiece
from factotum.model_gateway.providers.piper_local import PiperLocalAdapter
from factotum.model_gateway.providers.whisper_local import WhisperLocalAdapter
from factotum.model_gateway.testing import ask, collect_events, events, resolved


def audio(**changes: object) -> AudioPayload:
    fields: dict[str, object] = {"sample_rate": 16000, "data": b"some-wav-bytes"}
    return AudioPayload(**{**fields, **changes})  # type: ignore[arg-type]


def wav_bytes(sample_rate: int = 22050) -> bytes:
    buffer = BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\x00\x00" * 10)
    return buffer.getvalue()


def whisper_binding(
    model: str = "base", *, options: BindingOptions | None = None
) -> ResolvedBinding:
    binding = make_model_binding(
        tenant_id=make_tenant_id(),
        provider=ProviderKind.WHISPER_LOCAL,
        model=model,
        base_url=None,
        secret_ref=None,
        options=options,
    )
    return resolved(binding)


def piper_binding(model: str = "it_IT-paola-medium") -> ResolvedBinding:
    binding = make_model_binding(
        tenant_id=make_tenant_id(),
        provider=ProviderKind.PIPER_LOCAL,
        model=model,
        base_url=None,
        secret_ref=None,
    )
    return resolved(binding)


class FakeWhisperEngine:
    def __init__(self, outcome: str | Exception = "how do I reset the router") -> None:
        self._outcome = outcome
        self.asked: list[bytes] = []
        self.languages: list[tuple[str, ...] | None] = []
        self.vocabularies: list[tuple[str, ...]] = []

    def transcribe_wav(
        self,
        wav_bytes: bytes,
        languages: tuple[str, ...] | None,
        vocabulary: tuple[str, ...] = (),
    ) -> str:
        self.asked.append(wav_bytes)
        self.languages.append(languages)
        self.vocabularies.append(vocabulary)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class FakePiperEngine:
    """A voice: it makes a clip, or the pieces it was given a sentence at a time, and remembers on
    which thread it made each, how many it has made and what it was asked."""

    def __init__(
        self,
        outcome: bytes | Exception | None = None,
        *,
        pieces: Sequence[SpeechPiece | Exception] = (),
        fails_to_begin: Exception | None = None,
    ) -> None:
        self._outcome = outcome if outcome is not None else wav_bytes()
        self._pieces = list(pieces)
        self._fails_to_begin = fails_to_begin
        self.asked: list[str] = []
        self.asked_in_pieces: list[str] = []
        self.made = 0
        self.threads: list[int] = []

    def synthesize_wav(self, text: str) -> bytes:
        self.asked.append(text)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome

    def synthesize_pieces(self, text: str) -> Iterator[SpeechPiece]:
        self.asked_in_pieces.append(text)
        if self._fails_to_begin is not None:
            raise self._fails_to_begin
        return self._make()

    def _make(self) -> Iterator[SpeechPiece]:
        for item in self._pieces:
            self.threads.append(threading.get_ident())
            if isinstance(item, Exception):
                raise item
            self.made += 1
            yield item


def sentence(n: int, rate: int = 22050) -> SpeechPiece:
    """A sentence of a voice: ``n`` samples that are not like the ones of any other ``n``."""
    return SpeechPiece(sample_rate=rate, pcm=bytes((n + i) % 256 for i in range(2 * n)))


class TestWhisperLocalAdapter:
    async def test_the_words_of_the_binding_reach_the_engine_and_none_is_none(self) -> None:
        engine = FakeWhisperEngine()
        adapter = WhisperLocalAdapter(engine_factory=lambda model: engine)
        listed = whisper_binding(options=BindingOptions(vocabulary=("garage", "router")))

        await adapter.transcribe(None, listed, audio())  # type: ignore[arg-type]
        await adapter.transcribe(None, whisper_binding(), audio())  # type: ignore[arg-type]

        assert engine.vocabularies == [("garage", "router"), ()]

    async def test_it_transcribes_with_the_engine_of_the_bindings_model(self) -> None:
        engine = FakeWhisperEngine("hold the button for ten seconds")
        adapter = WhisperLocalAdapter(engine_factory=lambda model: engine)

        text = await adapter.transcribe(None, whisper_binding(), audio(data=b"raw-wav-bytes"))  # type: ignore[arg-type]

        assert text == "hold the button for ten seconds"
        assert engine.asked == [b"raw-wav-bytes"]

    @pytest.mark.parametrize("languages", [None, ("it",), ("it", "en"), ("en",)])
    async def test_the_languages_the_binding_allows_go_to_the_engine(
        self, languages: tuple[str, ...] | None
    ) -> None:
        engine = FakeWhisperEngine()
        adapter = WhisperLocalAdapter(engine_factory=lambda model: engine)
        binding = whisper_binding(options=BindingOptions(languages=languages))

        await adapter.transcribe(None, binding, audio())  # type: ignore[arg-type]

        assert engine.languages == [languages]

    async def test_a_binding_that_says_nothing_of_languages_is_listened_to_in_italian(self) -> None:
        engine = FakeWhisperEngine()
        adapter = WhisperLocalAdapter(engine_factory=lambda model: engine)

        await adapter.transcribe(None, whisper_binding(), audio())  # type: ignore[arg-type]

        assert engine.languages == [("it",)]

    async def test_an_engine_is_built_once_per_model_size_and_reused(self) -> None:
        built: list[str] = []

        def factory(model: str) -> FakeWhisperEngine:
            built.append(model)
            return FakeWhisperEngine()

        adapter = WhisperLocalAdapter(engine_factory=factory)

        await adapter.transcribe(None, whisper_binding("base"), audio())  # type: ignore[arg-type]
        await adapter.transcribe(None, whisper_binding("base"), audio())  # type: ignore[arg-type]
        await adapter.transcribe(None, whisper_binding("small"), audio())  # type: ignore[arg-type]

        assert built == ["base", "small"]

    async def test_a_failure_of_the_engine_is_a_typed_provider_error(self) -> None:
        engine = FakeWhisperEngine(RuntimeError("the model could not be loaded"))
        adapter = WhisperLocalAdapter(engine_factory=lambda model: engine)

        with pytest.raises(ProviderUnavailableError) as caught:
            await adapter.transcribe(None, whisper_binding(), audio())  # type: ignore[arg-type]

        assert caught.value.details["provider"] == "whisper_local"

    async def test_nothing_but_speech_to_text_is_offered(self) -> None:
        adapter = WhisperLocalAdapter(engine_factory=lambda model: FakeWhisperEngine())
        binding = whisper_binding()

        with pytest.raises(RoleNotSupportedError):
            await adapter.chat(None, binding, ask())  # type: ignore[arg-type]
        with pytest.raises(RoleNotSupportedError):
            await events(adapter.chat_stream(None, binding, ask()))  # type: ignore[arg-type]
        with pytest.raises(RoleNotSupportedError):
            await adapter.embed(None, binding, ["a"])  # type: ignore[arg-type]
        with pytest.raises(RoleNotSupportedError):
            await adapter.synthesize(None, binding, "hello")  # type: ignore[arg-type]
        with pytest.raises(RoleNotSupportedError):
            await events(adapter.synthesize_stream(None, binding, "hello"))  # type: ignore[arg-type]


class TestPiperLocalAdapter:
    async def test_it_synthesizes_with_the_engine_of_the_bindings_voice(self) -> None:
        clip = wav_bytes(sample_rate=22050)
        engine = FakePiperEngine(clip)
        adapter = PiperLocalAdapter(engine_factory=lambda voice: engine)

        result = await adapter.synthesize(None, piper_binding(), "Dinner is ready")  # type: ignore[arg-type]

        assert engine.asked == ["Dinner is ready"]
        assert result.sample_rate == 22050
        assert result.data == clip

    async def test_an_engine_is_built_once_per_voice_and_reused(self) -> None:
        built: list[str] = []

        def factory(voice: str) -> FakePiperEngine:
            built.append(voice)
            return FakePiperEngine()

        adapter = PiperLocalAdapter(engine_factory=factory)

        await adapter.synthesize(None, piper_binding("it_IT-paola-medium"), "a")  # type: ignore[arg-type]
        await adapter.synthesize(None, piper_binding("it_IT-paola-medium"), "b")  # type: ignore[arg-type]
        await adapter.synthesize(None, piper_binding("en_US-lessac-medium"), "c")  # type: ignore[arg-type]

        assert built == ["it_IT-paola-medium", "en_US-lessac-medium"]

    async def test_a_failure_of_the_engine_is_a_typed_provider_error(self) -> None:
        engine = FakePiperEngine(RuntimeError("the voice could not be loaded"))
        adapter = PiperLocalAdapter(engine_factory=lambda voice: engine)

        with pytest.raises(ProviderUnavailableError) as caught:
            await adapter.synthesize(None, piper_binding(), "hello")  # type: ignore[arg-type]

        assert caught.value.details["provider"] == "piper_local"

    async def test_nothing_but_text_to_speech_is_offered(self) -> None:
        adapter = PiperLocalAdapter(engine_factory=lambda voice: FakePiperEngine())
        binding = piper_binding()

        with pytest.raises(RoleNotSupportedError):
            await adapter.chat(None, binding, ask())  # type: ignore[arg-type]
        with pytest.raises(RoleNotSupportedError):
            await events(adapter.chat_stream(None, binding, ask()))  # type: ignore[arg-type]
        with pytest.raises(RoleNotSupportedError):
            await adapter.embed(None, binding, ["a"])  # type: ignore[arg-type]
        with pytest.raises(RoleNotSupportedError):
            await adapter.transcribe(None, binding, audio())  # type: ignore[arg-type]


class TestPiperLocalAdapterStreaming:
    async def test_a_sentence_at_a_time_is_what_the_voice_makes_and_what_comes_out(self) -> None:
        pieces = [sentence(10), sentence(20), sentence(30)]
        engine = FakePiperEngine(pieces=pieces)
        adapter = PiperLocalAdapter(engine_factory=lambda voice: engine)

        got = await events(adapter.synthesize_stream(None, piper_binding(), "Uno. Due. Tre."))  # type: ignore[arg-type]

        assert got == pieces
        assert engine.asked_in_pieces == ["Uno. Due. Tre."]
        assert engine.asked == []

    async def test_a_sentence_is_made_when_it_is_asked_for_and_not_before(self) -> None:
        engine = FakePiperEngine(pieces=[sentence(10), sentence(20), sentence(30)])
        adapter = PiperLocalAdapter(engine_factory=lambda voice: engine)
        stream = adapter.synthesize_stream(None, piper_binding(), "Uno. Due. Tre.")  # type: ignore[arg-type]

        assert engine.made == 0
        await anext(stream)
        assert engine.made == 1
        await anext(stream)
        assert engine.made == 2
        await stream.aclose()

    async def test_a_listener_that_goes_away_asks_the_voice_for_no_more(self) -> None:
        engine = FakePiperEngine(pieces=[sentence(10), sentence(20), sentence(30)])
        adapter = PiperLocalAdapter(engine_factory=lambda voice: engine)
        stream = adapter.synthesize_stream(None, piper_binding(), "Uno. Due. Tre.")  # type: ignore[arg-type]

        await anext(stream)
        await stream.aclose()

        assert engine.made == 1

    async def test_every_sentence_is_made_off_the_thread_that_waits_for_it(self) -> None:
        engine = FakePiperEngine(pieces=[sentence(10), sentence(20)])
        adapter = PiperLocalAdapter(engine_factory=lambda voice: engine)

        await events(adapter.synthesize_stream(None, piper_binding(), "Uno. Due."))  # type: ignore[arg-type]

        assert len(engine.threads) == 2
        assert threading.get_ident() not in engine.threads

    async def test_a_voice_with_nothing_to_say_is_a_stream_with_nothing_in_it(self) -> None:
        engine = FakePiperEngine(pieces=[])
        adapter = PiperLocalAdapter(engine_factory=lambda voice: engine)

        assert await events(adapter.synthesize_stream(None, piper_binding(), "...")) == []  # type: ignore[arg-type]

    async def test_a_failure_before_the_first_sentence_is_a_typed_provider_error(self) -> None:
        engine = FakePiperEngine(pieces=[RuntimeError("the voice could not be loaded")])
        adapter = PiperLocalAdapter(engine_factory=lambda voice: engine)

        with pytest.raises(ProviderUnavailableError) as caught:
            await events(adapter.synthesize_stream(None, piper_binding(), "hello"))  # type: ignore[arg-type]

        assert caught.value.details["provider"] == "piper_local"
        assert "could not be loaded" not in str(caught.value)
        assert "RuntimeError" in str(caught.value)

    async def test_a_voice_that_cannot_even_begin_is_the_same_error(self) -> None:
        engine = FakePiperEngine(fails_to_begin=OSError("no such voice"))
        adapter = PiperLocalAdapter(engine_factory=lambda voice: engine)

        with pytest.raises(ProviderUnavailableError, match="OSError"):
            await events(adapter.synthesize_stream(None, piper_binding(), "hello"))  # type: ignore[arg-type]

    async def test_a_failure_after_some_sentences_ends_the_stream_with_that_error(self) -> None:
        engine = FakePiperEngine(pieces=[sentence(10), RuntimeError("the voice fell over")])
        adapter = PiperLocalAdapter(engine_factory=lambda voice: engine)

        got, error = await collect_events(
            adapter.synthesize_stream(None, piper_binding(), "Uno. Due.")  # type: ignore[arg-type]
        )

        assert got == [sentence(10)]
        assert isinstance(error, ProviderUnavailableError)

    async def test_one_voice_makes_a_clip_and_a_stream_for_every_text_it_is_asked(self) -> None:
        built: list[str] = []
        engine = FakePiperEngine(pieces=[sentence(10)])

        def factory(voice: str) -> FakePiperEngine:
            built.append(voice)
            return engine

        adapter = PiperLocalAdapter(engine_factory=factory)

        await adapter.synthesize(None, piper_binding(), "a")  # type: ignore[arg-type]
        await events(adapter.synthesize_stream(None, piper_binding(), "b"))  # type: ignore[arg-type]

        assert built == ["it_IT-paola-medium"]
        assert (engine.asked, engine.asked_in_pieces) == (["a"], ["b"])
