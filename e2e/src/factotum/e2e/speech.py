"""Real speech for the end-to-end tests: faster-whisper and Piper, the engines that are not made up,
behind the real adapters of the gateway, and a person who talks to a device with a voice of Piper's.

Nothing is ever downloaded here. The models are looked for where they already are, and what is not
found is a skip: the folder of a Whisper model (``FACTOTUM_LIVE_WHISPER_MODEL``, or the one of the
Hugging Face cache that ``Systran/faster-whisper-small`` already made) and the ``.onnx`` file of a
Piper voice (``FACTOTUM_LIVE_PIPER_VOICE``, or the Italian one in the cache of the adapter). The
engines are the ones the adapters build, and are built once for all the tests that use them: loading
a model costs seconds.

``FACTOTUM_LIVE_SPEECH_DEVICE`` says where Whisper runs (``cpu``, which is slower and is everywhere,
or ``cuda``).
"""

import io
import os
import wave
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from factotum.contracts.audio import AudioPayload
from factotum.model_gateway.providers.piper_local import LocalPiperEngine, PiperEngine
from factotum.model_gateway.providers.whisper_local import FasterWhisperEngine, WhisperEngine

WHISPER_VARIABLE = "FACTOTUM_LIVE_WHISPER_MODEL"
VOICE_VARIABLE = "FACTOTUM_LIVE_PIPER_VOICE"
DEVICE_VARIABLE = "FACTOTUM_LIVE_SPEECH_DEVICE"
CUDA_VARIABLE = "FACTOTUM_CUDA_DLL_DIR"

DEFAULT_VOICE = "it_IT-paola-medium.onnx"
MICROPHONE_RATE = 16_000
"""What a microphone hands over (Porcupine's rate): the voice of Piper is brought to it."""

_WHISPER_FILES = ("model.bin", "config.json", "tokenizer.json")


@dataclass(frozen=True)
class RealSpeech:
    """Where the real models are, and where Whisper runs."""

    whisper: Path
    """The folder of a faster-whisper model: a path is never downloaded, a name is."""

    voice: Path
    """The ``.onnx`` file of a Piper voice, with its ``.onnx.json`` beside it."""

    device: str = "cpu"
    languages: tuple[str, ...] = ("it",)
    """What the house is spoken to in, as the speech-to-text binding says it."""


def _is_whisper_model(folder: Path) -> bool:
    return all((folder / name).is_file() for name in _WHISPER_FILES)


def find_speech(
    environ: MutableMapping[str, str] | None = None, home: Path | None = None
) -> RealSpeech | None:
    """The models on this machine, or None when either is missing (the caller skips)."""
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else home

    given = environ.get(WHISPER_VARIABLE)
    whisper: Path | None = None
    if given:
        # A name (``small``) is not a folder: what is not on disk is not found, not fetched.
        whisper = Path(given) if _is_whisper_model(Path(given)) else None
    else:
        snapshots = home / ".cache/huggingface/hub/models--Systran--faster-whisper-small/snapshots"
        found = sorted(
            (folder for folder in snapshots.glob("*") if _is_whisper_model(folder)),
            key=lambda folder: folder.stat().st_mtime,
        )
        whisper = found[-1] if found else None

    voice = Path(environ.get(VOICE_VARIABLE) or home / ".cache/factotum/piper" / DEFAULT_VOICE)
    has_voice = (
        voice.suffix == ".onnx"
        and voice.is_file()
        and voice.with_name(f"{voice.name}.json").is_file()
    )
    if whisper is None or not has_voice:
        return None
    return RealSpeech(whisper=whisper, voice=voice, device=environ.get(DEVICE_VARIABLE, "cpu"))


def ensure_cuda_libraries(
    speech: RealSpeech,
    environ: MutableMapping[str, str] | None = None,
    register: Callable[[str], object] | None = None,
) -> Path | None:
    """What ctranslate2 needs to run on the graphics card and does not bring: cuBLAS 12, on the
    ``PATH`` (cuDNN 9 comes inside its wheel). Where to find it is ``FACTOTUM_CUDA_DLL_DIR``, or the
    folder that Ollama keeps its own in; the folder is put on the ``PATH`` of this process, and
    nothing is installed or copied. None where it is not needed or not found."""
    if speech.device != "cuda":
        return None
    environ = os.environ if environ is None else environ
    register = register or getattr(os, "add_dll_directory", None)
    candidates = [environ.get(CUDA_VARIABLE)]
    if app_data := environ.get("LOCALAPPDATA"):
        candidates.append(str(Path(app_data) / "Programs/Ollama/lib/ollama/cuda_v12"))
    for candidate in candidates:
        if candidate and (Path(candidate) / "cublas64_12.dll").is_file():
            environ["PATH"] = f"{candidate}{os.pathsep}{environ.get('PATH', '')}"
            if register is not None:
                register(candidate)
            return Path(candidate)
    return None


_whispers: dict[tuple[Path, str], FasterWhisperEngine] = {}
_voices: dict[Path, LocalPiperEngine] = {}
_people: dict[Path, Any] = {}


def real_whisper(speech: RealSpeech) -> FasterWhisperEngine:
    """The engine of Whisper, the same one for every test: built here, loaded by its first use."""
    key = (speech.whisper, speech.device)
    if key not in _whispers:
        _whispers[key] = FasterWhisperEngine(
            str(speech.whisper), device=speech.device, cache_dir=None
        )
    return _whispers[key]


def real_piper(speech: RealSpeech) -> LocalPiperEngine:
    if speech.voice not in _voices:
        _voices[speech.voice] = LocalPiperEngine(str(speech.voice), cache_dir=None, use_cuda=False)
    return _voices[speech.voice]


def silence(seconds: float = 0.5, rate: int = MICROPHONE_RATE) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(rate)
        wav_file.writeframes(bytes(2 * int(seconds * rate)))
    return buffer.getvalue()


def load(speech: RealSpeech) -> None:
    """Both models, loaded now: what the first test to use them would otherwise wait for."""
    ensure_cuda_libraries(speech)
    real_whisper(speech).transcribe_wav(silence(), speech.languages)
    real_piper(speech).synthesize_wav("Ciao.")


class RecordedWhisper:
    """The real engine of Whisper under a recorder, so that a test sees of it what it sees of the
    stand-in: which model was asked for, in which languages, and what was heard."""

    def __init__(self, engine: WhisperEngine) -> None:
        self._engine = engine
        self.models: list[str] = []
        self.heard: list[str] = []
        self.languages: list[tuple[str, ...] | None] = []
        self.broken = False

    def build(self, model: str) -> "RecordedWhisper":
        self.models.append(model)
        return self

    def transcribe_wav(self, wav_bytes: bytes, languages: tuple[str, ...] | None) -> str:
        if self.broken:
            raise RuntimeError("the model fell over")
        heard = self._engine.transcribe_wav(wav_bytes, languages)
        self.languages.append(languages)
        self.heard.append(heard)
        return heard


class RecordedPiper:
    """The real voice of Piper under a recorder: which voice was asked for, and what it was asked
    to say."""

    def __init__(self, engine: PiperEngine) -> None:
        self._engine = engine
        self.voices: list[str] = []
        self.spoken: list[str] = []

    def build(self, voice: str) -> "RecordedPiper":
        self.voices.append(voice)
        return self

    def synthesize_wav(self, text: str) -> bytes:
        self.spoken.append(text)
        return self._engine.synthesize_wav(text)


class Person:
    """Somebody who talks to a device. Their voice is a voice of Piper's, loaded for them alone, and
    it says the same words the same way every time (the noise of the generator is off); what they
    say reaches the device as a microphone would hand it over, at 16 kHz."""

    def __init__(self, speech: RealSpeech) -> None:
        self._speech = speech

    def saying(self, words: str) -> AudioPayload:
        from faster_whisper.audio import decode_audio
        from piper import SynthesisConfig

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            self._voice().synthesize_wav(
                words, wav_file, syn_config=SynthesisConfig(noise_scale=0.0, noise_w_scale=0.0)
            )
        samples = decode_audio(io.BytesIO(buffer.getvalue()), sampling_rate=MICROPHONE_RATE)
        pcm = (samples * 32767).astype("<i2").tobytes()
        out = io.BytesIO()
        with wave.open(out, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(MICROPHONE_RATE)
            wav_file.writeframes(pcm)
        return AudioPayload(sample_rate=MICROPHONE_RATE, data=out.getvalue())

    def _voice(self) -> Any:
        if self._speech.voice not in _people:
            from piper import PiperVoice

            _people[self._speech.voice] = PiperVoice.load(str(self._speech.voice))
        return _people[self._speech.voice]


def hears(speech: RealSpeech, clip: AudioPayload) -> str:
    """What somebody who listens to a clip makes of it: the same Whisper, in the languages of the
    house."""
    return real_whisper(speech).transcribe_wav(clip.data, speech.languages)
