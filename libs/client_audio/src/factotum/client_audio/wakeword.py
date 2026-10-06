"""The engines that watch a microphone for a word (ADR 0027).

``factotum.client_audio.WakeWord`` asks an engine, a frame of sound at a time, whether the word was
said; what an engine is made of is its own business. There are two:

* **openWakeWord**, which needs no account and no key. Three small models of ONNX run one after the
  other (the spectrogram of the sound, the embedding of what a window of it says, and the model of
  the word that judges the last sixteen embeddings), and the runtime here is the one that its author
  published (https://github.com/dscripka/openWakeWord, Apache 2.0), written again on ``onnxruntime``
  and ``numpy`` alone: the package asks for ``scikit-learn``, ``scipy`` and, on Linux, a TensorFlow
  Lite runtime that has no build for the Python that this project is written in. The models are not
  in this repository, and are not downloaded by anything that does not say so: they are the
  author's (CC BY-NC-SA 4.0, for use that is not commercial), and ``factotum-device wake-word
  download`` is how a person has them, checked against the hashes below.
* **Porcupine**, which needs a Picovoice AccessKey. Picovoice ended its free plan on 2026-06-30 and
  has none for a person, so this is for whoever has one.
"""

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray

DEFAULT_WAKE_WORD = "jarvis"
"""One of Porcupine's built-in keywords (``pvporcupine.KEYWORDS``); there is no "factotum" one."""

DEFAULT_OPENWAKEWORD = "hey_jarvis"
"""One of the words that openWakeWord has a model for: they are English, and a person who says one
in Italian is understood as far as the accent allows."""

ACCESS_KEY_VARIABLE = "PICOVOICE_ACCESS_KEY"
"""The AccessKey of Porcupine, which Picovoice gives to a company (there is no free one)."""

SAMPLE_RATE = 16_000
FRAME_LENGTH = 1_280
"""Eighty milliseconds: what openWakeWord takes at a time."""

THRESHOLD = 0.5
"""The score of a word, from 0 to 1, at which it is taken to have been said: the one its author
suggests. Higher is fewer mistakes and more words that are not heard."""

RELEASE = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1"
"""Where the models are, in a release that does not change."""

_CONTEXT = 3 * 160
"""The samples before a frame that its spectrogram needs (three steps of 10 ms): the window of the
spectrogram is longer than a step."""

_MEL_ROWS = 76
"""The rows of a spectrogram that one embedding is made of."""

_START_NOISE_SECONDS = 4
"""The embeddings that the engine starts from are those of this much noise: a buffer that is empty
or all zeros is a sound that does not exist, and the models were not shown it."""

_FEATURES_KEPT = 120


class Engine(StrEnum):
    """What watches for the word."""

    OPENWAKEWORD = "openwakeword"
    """openWakeWord: no account and no key. Its models are fetched once by
    ``factotum-device wake-word download``, and its words are English."""

    PORCUPINE = "porcupine"
    """Porcupine: it needs a Picovoice AccessKey, which Picovoice gives to companies (its free plan
    ended on 2026-06-30)."""


def default_word(engine: Engine) -> str:
    """The word that an engine listens for when nobody says which."""
    return DEFAULT_OPENWAKEWORD if engine is Engine.OPENWAKEWORD else DEFAULT_WAKE_WORD


class WakeWordError(Exception):
    """A wake word cannot be had: it is not one that there is a model for, or the models are not
    where they should be, or the engine wants a key that was not given."""


class ModelsNotThereError(WakeWordError):
    """The models of a word are not in the folder where they are looked for (or are not what they
    should be): ``command`` is what a person runs to have them."""

    def __init__(
        self, word: str, directory: Path, missing: Sequence["ModelFile"], command: str
    ) -> None:
        self.word = word
        self.directory = directory
        self.missing = tuple(missing)
        self.command = command
        super().__init__(
            f"the models of {word!r} are not in {directory}: "
            f"{', '.join(file.name for file in missing)} (``{command}`` brings them)"
        )


class WakeWordEngine(Protocol):
    """What ``WakeWord`` asks of whatever watches for the word."""

    sample_rate: int
    """The rate that the microphone is opened at."""

    frame_length: int
    """The samples that ``process`` is given at a time."""

    def process(self, frame: Any) -> bool:
        """Whether the word was said, as far as this frame can tell."""
        ...

    def reset(self) -> None:
        """Forget what was heard: what comes next is not a continuation of it."""
        ...

    def close(self) -> None: ...


# ---- the models that openWakeWord has ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelFile:
    """A file of a model, as the release has it: its size and its SHA-256, so that what is on a disk
    is known to be what was meant."""

    name: str
    size: int
    sha256: str

    @property
    def url(self) -> str:
        return f"{RELEASE}/{self.name}"


MELSPECTROGRAM = ModelFile(
    "melspectrogram.onnx",
    1_087_958,
    "ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f",
)
EMBEDDING = ModelFile(
    "embedding_model.onnx",
    1_326_578,
    "70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f",
)
WAKE_WORDS: Mapping[str, ModelFile] = {
    "alexa": ModelFile(
        "alexa_v0.1.onnx",
        854_246,
        "6ff566a01d12670e8d9e3c59da32651db1575d17272a601b7f8a39283dfbae3e",
    ),
    "hey_jarvis": ModelFile(
        "hey_jarvis_v0.1.onnx",
        1_271_370,
        "94a13cfe60075b132f6a472e7e462e8123ee70861bc3fb58434a73712ee0d2cb",
    ),
    "hey_mycroft": ModelFile(
        "hey_mycroft_v0.1.onnx",
        857_691,
        "c2a311e8fa1338de89c31b3b46dc4dffd4af2f9a8d6ddead48893c2d301b1f18",
    ),
    "hey_rhasspy": ModelFile(
        "hey_rhasspy_v0.1.onnx",
        204_081,
        "5a9b3ed3be2910e35780e097905aa9f35a9c10038df47914cf2b3ec4d670f6ea",
    ),
}
"""The words that openWakeWord has a model for."""


def default_models_directory() -> Path:
    """Where the models are looked for when nobody says: next to the voices of Piper."""
    return Path.home() / ".cache" / "factotum" / "openwakeword"


class WakeWordModels:
    """The folder that the models of openWakeWord are in, and which of them are there.

    ``words`` is the catalogue of the words that it knows (the models of the release, unless a test
    gives its own)."""

    def __init__(
        self,
        directory: Path | None = None,
        *,
        words: Mapping[str, ModelFile] = WAKE_WORDS,
        shared: Sequence[ModelFile] = (MELSPECTROGRAM, EMBEDDING),
    ) -> None:
        self.directory = directory if directory is not None else default_models_directory()
        self._words = words
        self._shared = tuple(shared)

    def files_for(self, word: str) -> list[ModelFile]:
        """What the word needs: the two models that every word shares, and its own."""
        if word not in self._words:
            raise WakeWordError(
                f"there is no model for {word!r}: the words are {', '.join(sorted(self._words))}"
            )
        return [*self._shared, self._words[word]]

    def path_of(self, file: ModelFile) -> Path:
        return self.directory / file.name

    def holds(self, file: ModelFile) -> bool:
        """Whether the file is there, and is what it should be (a half of a download, or another
        file with its name, is not)."""
        path = self.path_of(file)
        try:
            if path.stat().st_size != file.size:
                return False
            return hashlib.sha256(path.read_bytes()).hexdigest() == file.sha256
        except OSError:
            return False

    def missing(self, word: str) -> list[ModelFile]:
        """The files of the word that are not there, or are not what they should be."""
        return [file for file in self.files_for(word) if not self.holds(file)]

    def download_command(self, word: str) -> str:
        """What a person runs to have the models of the word in this folder: the folder is said only
        when it is not the one that is looked in when nobody says."""
        folder = (
            "" if self.directory == default_models_directory() else f' --dir "{self.directory}"'
        )
        return f"factotum-device wake-word download {word}{folder}"


# ---- the runtime of openWakeWord -----------------------------------------------------------------


class Session(Protocol):
    """What ``onnxruntime.InferenceSession`` offers, which is all that the engine uses of it."""

    def run(self, output_names: Any, input_feed: Mapping[str, Any]) -> Sequence[Any]: ...

    def get_inputs(self) -> Sequence[Any]: ...


def load_session(path: Path) -> Session:
    """An ONNX model for the processor, with one thread: a frame of eighty milliseconds does not
    need more, and a device that listens all day should not take a core for it."""
    import onnxruntime  # heavy: deferred to the first use

    options = onnxruntime.SessionOptions()
    options.inter_op_num_threads = 1
    options.intra_op_num_threads = 1
    session: Session = onnxruntime.InferenceSession(
        str(path), sess_options=options, providers=["CPUExecutionProvider"]
    )
    return session


class OpenWakeWordEngine:
    """openWakeWord, for one word, a frame of 80 ms at a time.

    A frame is 1280 samples of 16-bit sound at 16 kHz. The runtime keeps the last 1760 samples (the
    frame and what its spectrogram needs before it), the last 76 rows of the spectrogram, and the
    embeddings of the last ten seconds; each frame adds eight rows to the spectrogram and one
    embedding, and the model of the word judges the last sixteen embeddings. The first five scores
    after a start are zero (the buffers hold noise until then), which is what the package does too.

    The models are given as ``Session``s so that a test can give others.
    """

    sample_rate = SAMPLE_RATE
    frame_length = FRAME_LENGTH

    def __init__(
        self,
        melspectrogram: Session,
        embedding: Session,
        wake_word: Session,
        *,
        threshold: float = THRESHOLD,
        patience: int = 1,
        seed: int = 0,
    ) -> None:
        if not 0 < threshold <= 1:
            raise ValueError("the threshold is a score, above 0 and at most 1")
        if patience < 1:
            raise ValueError("patience is a number of frames, one or more")
        self._melspectrogram = melspectrogram
        self._embedding = embedding
        self._wake_word = wake_word
        self._threshold = threshold
        self._patience = patience
        self._mel_input = melspectrogram.get_inputs()[0].name
        self._embedding_input = embedding.get_inputs()[0].name
        word_input = wake_word.get_inputs()[0]
        self._word_input = str(word_input.name)
        frames = word_input.shape[1]
        if not isinstance(frames, int) or frames < 1:
            raise WakeWordError("the model of the word does not say how many embeddings it takes")
        self._frames = frames
        noise = (
            np.random.RandomState(seed)
            .randint(-1000, 1000, SAMPLE_RATE * _START_NOISE_SECONDS)
            .astype(np.int16)
        )
        self._start_features = self._embeddings_of(noise)
        self.reset()

    def reset(self) -> None:
        self._samples: NDArray[np.int16] = np.empty(0, dtype=np.int16)
        self._spectrogram: NDArray[np.float32] = np.ones((_MEL_ROWS, 32), dtype=np.float32)
        self._features: NDArray[np.float32] = self._start_features.copy()
        self._scores_made = 0
        self._over = 0

    def close(self) -> None:
        """Nothing is held that a garbage collector does not take."""

    def process(self, frame: Any) -> bool:
        samples = np.asarray(frame)
        if samples.dtype != np.int16 or samples.shape != (FRAME_LENGTH,):
            raise ValueError(
                f"a frame is {FRAME_LENGTH} samples of 16-bit sound, not "
                f"{samples.shape} of {samples.dtype}"
            )
        score = self.score(samples)
        self._over = self._over + 1 if score >= self._threshold else 0
        return self._over >= self._patience

    def score(self, samples: NDArray[np.int16]) -> float:
        """How much the last sixteen embeddings, with this frame's, are the word: from 0 to 1."""
        self._samples = np.concatenate((self._samples, samples))[-(FRAME_LENGTH + _CONTEXT) :]
        rows = self._spectrogram_of(self._samples)
        self._spectrogram = np.vstack((self._spectrogram, rows))[-_MEL_ROWS:]
        window = self._spectrogram[None, :, :, None].astype(np.float32)
        embedding = np.atleast_2d(
            np.squeeze(self._run(self._embedding, self._embedding_input, window))
        )
        self._features = np.vstack((self._features, embedding))[-_FEATURES_KEPT:]
        features = self._features[-self._frames :][None, :, :].astype(np.float32)
        score = float(
            np.asarray(self._run(self._wake_word, self._word_input, features)).reshape(-1)[0]
        )
        if self._scores_made < 5:
            score = 0.0
        self._scores_made += 1
        return score

    @staticmethod
    def _run(session: Session, name: str, value: NDArray[Any]) -> Any:
        return session.run(None, {name: value})[0]

    def _spectrogram_of(self, samples: NDArray[np.int16]) -> NDArray[np.float32]:
        """The mel spectrogram of the sound, as the embedding model wants it: one row for each ten
        milliseconds, brought near what Google's model made of the same sound."""
        out = self._run(self._melspectrogram, self._mel_input, samples.astype(np.float32)[None, :])
        spectrogram = np.atleast_2d(np.squeeze(out)) / 10 + 2
        return np.asarray(spectrogram, dtype=np.float32)

    def _embeddings_of(self, samples: NDArray[np.int16]) -> NDArray[np.float32]:
        """The embeddings of a stretch of sound: one for each window of 76 rows of its spectrogram,
        eight rows apart."""
        spectrogram = self._spectrogram_of(samples)
        windows = [
            spectrogram[start : start + _MEL_ROWS]
            for start in range(0, spectrogram.shape[0], 8)
            if spectrogram[start : start + _MEL_ROWS].shape[0] == _MEL_ROWS
        ]
        batch = np.expand_dims(np.array(windows, dtype=np.float32), axis=-1)
        made = self._run(self._embedding, self._embedding_input, batch)
        return np.asarray(np.atleast_2d(np.squeeze(made)), dtype=np.float32)


def open_wake_word_engine(
    models: WakeWordModels,
    word: str,
    *,
    threshold: float = THRESHOLD,
    patience: int = 1,
    load: Callable[[Path], Session] = load_session,
) -> Callable[[], WakeWordEngine]:
    """What makes the engine for ``word`` when it is called, from the models in ``models``. It says,
    when it is called and not before, if a model is not there and what to do about it; ``load``
    stands in for ``onnxruntime``, for a test."""
    needed = models.files_for(word)  # a word that there is no model for is said at once

    def make() -> WakeWordEngine:
        missing = models.missing(word)
        if missing:
            raise ModelsNotThereError(
                word, models.directory, missing, models.download_command(word)
            )
        melspectrogram, embedding, wake_word = (load(models.path_of(file)) for file in needed)
        return OpenWakeWordEngine(
            melspectrogram, embedding, wake_word, threshold=threshold, patience=patience
        )

    return make


# ---- Porcupine -----------------------------------------------------------------------------------


class PorcupineEngine:
    """Porcupine, as ``pvporcupine.create`` makes it: it takes frames and says which keyword they
    had in them, or that none."""

    def __init__(self, porcupine: Any) -> None:
        self._porcupine = porcupine
        self.sample_rate: int = porcupine.sample_rate
        self.frame_length: int = porcupine.frame_length

    def process(self, frame: Any) -> bool:
        return bool(self._porcupine.process(frame) >= 0)

    def reset(self) -> None:
        """Porcupine has no memory to speak of: a frame is a few words long."""

    def close(self) -> None:
        self._porcupine.delete()


def porcupine_engine(
    access_key: str, word: str = DEFAULT_WAKE_WORD, *, create: Callable[..., Any] | None = None
) -> Callable[[], WakeWordEngine]:
    """What makes the engine for ``word`` when it is called. The first time, Picovoice's service
    validates the key (a network call); the model of a keyword is in the ``pvporcupine`` package.
    ``create`` stands in for ``pvporcupine.create``, for a test."""

    def make() -> WakeWordEngine:
        factory = create
        if factory is None:
            try:
                import pvporcupine  # needs a key, and is not installed unless it is wanted
            except ImportError:
                raise WakeWordError(
                    "pvporcupine is not installed (and needs an AccessKey that Picovoice gives to "
                    "companies): listening.engine openwakeword needs neither"
                ) from None
            factory = pvporcupine.create
        return PorcupineEngine(factory(access_key=access_key, keywords=[word]))

    return make


# ---- the engine that a client asks for -----------------------------------------------------------


def wake_word_engine(
    engine: Engine,
    word: str,
    *,
    access_key: str | None = None,
    models: WakeWordModels | None = None,
    threshold: float = THRESHOLD,
    patience: int = 1,
    load: Callable[[Path], Session] = load_session,
) -> Callable[[], WakeWordEngine]:
    """What makes the engine that watches for ``word``, when it is called: the one place that knows
    which engine takes what, for every client. It raises ``WakeWordError`` at once for what cannot
    be (a word that there is no model for, Porcupine with no key); what is not there yet (the
    models) is said when the engine is made. ``models`` is where openWakeWord's are (the folder
    that nobody chose, unless it is given), and ``load`` stands in for ``onnxruntime``."""
    if engine is Engine.PORCUPINE:
        if access_key is None:
            raise WakeWordError(
                f"porcupine needs an access key (${ACCESS_KEY_VARIABLE}): Picovoice gives them to "
                f"companies and has no free plan, and openwakeword needs none"
            )
        return porcupine_engine(access_key, word)
    return open_wake_word_engine(
        models if models is not None else WakeWordModels(),
        word,
        threshold=threshold,
        patience=patience,
        load=load,
    )
