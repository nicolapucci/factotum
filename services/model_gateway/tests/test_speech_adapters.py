"""The two local speech adapters: faster-whisper (speech-to-text) and Piper (text-to-speech).

Neither adapter talks to a real model here: each takes an ``engine_factory`` that hands out a
fake standing in for ``faster_whisper.WhisperModel`` or ``piper.PiperVoice``, so these tests never
load a model or touch a GPU. What the engine of Whisper decides is tested in
``test_whisper_engine.py`` with a fake model; the models themselves are exercised only by the live
tests and by the end-to-end ones with real speech, by hand, against the real libraries.
"""

import wave
from io import BytesIO

import pytest

from factotum.contracts.audio import AudioPayload
from factotum.contracts.errors import ProviderUnavailableError, RoleNotSupportedError
from factotum.contracts.models import BindingOptions, ProviderKind
from factotum.contracts.testing import make_model_binding, make_tenant_id
from factotum.model_gateway.providers.base import ResolvedBinding
from factotum.model_gateway.providers.piper_local import PiperLocalAdapter
from factotum.model_gateway.providers.whisper_local import WhisperLocalAdapter
from factotum.model_gateway.testing import ask, events, resolved


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

    def transcribe_wav(self, wav_bytes: bytes, languages: tuple[str, ...] | None) -> str:
        self.asked.append(wav_bytes)
        self.languages.append(languages)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class FakePiperEngine:
    def __init__(self, outcome: bytes | Exception | None = None) -> None:
        self._outcome = outcome if outcome is not None else wav_bytes()
        self.asked: list[str] = []

    def synthesize_wav(self, text: str) -> bytes:
        self.asked.append(text)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class TestWhisperLocalAdapter:
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
