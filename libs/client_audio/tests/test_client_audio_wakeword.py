"""The engines that watch for a word (ADR 0027): the runtime of openWakeWord, which is tried here
with models that are made up (three that answer with what they were given, so that what flows from
one to the next can be read), the folder of models that it needs and the hashes that say they are
what they should be, and Porcupine, which is behind the same interface.

That the runtime gives the scores of the package that it was taken from, to the last digit, on
the clips that its author keeps, is measured with the real models (``test_e2e_wakeword.py``, live),
which are not in this repository.
"""

import hashlib
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import numpy as np
import pytest

from factotum.client_audio.testing import FakeWakeWord, rows_of_spectrogram
from factotum.client_audio.wakeword import (
    EMBEDDING,
    MELSPECTROGRAM,
    RELEASE,
    WAKE_WORDS,
    Engine,
    ModelFile,
    ModelsNotThereError,
    OpenWakeWordEngine,
    PorcupineEngine,
    WakeWordError,
    WakeWordModels,
    default_models_directory,
    default_word,
    load_session,
    open_wake_word_engine,
    porcupine_engine,
    wake_word_engine,
)

FRAME = 1_280


Models = FakeWakeWord
rows_of = rows_of_spectrogram


def started_with(seed: int) -> np.ndarray:
    """The sound that the buffers of an engine start from: four seconds of noise."""
    return np.random.RandomState(seed).randint(-1000, 1000, 64_000).astype(np.int16)


class TestWhatAFrameIs:
    @pytest.mark.parametrize(
        "frame",
        [
            np.zeros(512, dtype=np.int16),
            np.zeros(2_560, dtype=np.int16),
            np.zeros(FRAME, dtype=np.float32),
            np.zeros(FRAME, dtype=np.int32),
            np.zeros((FRAME, 1), dtype=np.int16),
            [0] * FRAME,
        ],
        ids=["short", "long", "float", "wide", "two-dimensional", "a list"],
    )
    def test_one_that_is_not_1280_samples_of_16_bit_sound_is_refused(self, frame: Any) -> None:
        models = Models()

        with pytest.raises(ValueError, match="1280 samples of 16-bit sound"):
            models.engine.process(frame)

        assert models.melspectrogram.asked[-1].shape == (1, 64_000)  # only what it started with

    def test_the_rate_and_the_length_are_what_the_microphone_is_to_be_opened_at(self) -> None:
        engine = Models().engine

        assert (engine.sample_rate, engine.frame_length) == (16_000, 1_280)


class TestTheFirstScores:
    def test_the_first_five_are_zero_whatever_the_model_says_for_the_buffers_hold_noise(
        self,
    ) -> None:
        models = Models(score=1.0)

        fired = [models.engine.process(models.frame()) for _ in range(8)]

        assert fired == [False] * 5 + [True] * 3

    def test_after_a_reset_it_is_so_again(self) -> None:
        models = Models(score=1.0)
        for _ in range(8):
            models.engine.process(models.frame())

        models.engine.reset()
        fired = [models.engine.process(models.frame()) for _ in range(7)]

        assert fired == [False] * 5 + [True] * 2


class TestWhatTheModelsAreAsked:
    def test_it_starts_from_four_seconds_of_noise_that_is_the_same_for_the_same_seed(self) -> None:
        models = Models(seed=7)

        [noise] = models.melspectrogram.asked
        assert noise.dtype == np.float32
        assert np.array_equal(noise[0], started_with(7).astype(np.float32))
        other = Models(seed=8).melspectrogram.asked[0]
        assert not np.array_equal(noise, other)

    def test_the_embeddings_it_starts_from_are_those_of_the_noise_and_not_zeros(self) -> None:
        models = Models(seed=3)
        noise = started_with(3).astype(np.float32)
        expected = noise.mean() / 10 + 2  # what the made-up models make of it

        features = models.engine._features

        assert features.shape == (41, 96)
        assert np.allclose(features, expected)
        assert abs(expected) > 1  # it is not near zero, for this to say something

    def test_the_noise_is_made_into_windows_of_76_rows_eight_apart(self) -> None:
        models = Models()

        [windows] = models.embedding.asked

        assert rows_of(64_000) == 397
        assert windows.shape == (41, 76, 32, 1)  # (397 - 76) // 8 + 1: a short one is not taken
        assert windows.dtype == np.float32

    def test_the_first_frame_is_given_to_the_spectrogram_as_it_is_and_alone(self) -> None:
        models = Models()
        frame = np.arange(FRAME, dtype=np.int16)

        models.engine.process(frame)

        asked = models.melspectrogram.asked[-1]
        assert asked.shape == (1, FRAME)
        assert asked.dtype == np.float32
        assert np.array_equal(asked[0], np.arange(FRAME, dtype=np.float32))  # not made smaller

    def test_the_next_ones_come_with_the_480_samples_before_them(self) -> None:
        models = Models()
        first = np.arange(FRAME, dtype=np.int16)
        second = (np.arange(FRAME) + 5_000).astype(np.int16)
        third = (np.arange(FRAME) + 9_000).astype(np.int16)

        for frame in (first, second, third):
            models.engine.process(frame)

        _, one, two = models.melspectrogram.asked[-3:]
        assert one.shape == (1, FRAME + 480)
        assert np.array_equal(one[0, :480], first[-480:].astype(np.float32))
        assert np.array_equal(one[0, 480:], second.astype(np.float32))
        assert two.shape == (1, FRAME + 480)  # it does not grow: the past is a few steps long
        assert np.array_equal(two[0, :480], second[-480:].astype(np.float32))

    def test_the_embedding_is_made_of_the_last_76_rows_with_the_rows_of_the_start_before(
        self,
    ) -> None:
        models = Models()

        models.engine.process(models.frame(fill=100))

        window = models.embedding.asked[-1]
        assert window.shape == (1, 76, 32, 1)
        new = rows_of(FRAME)  # five rows for the first frame
        assert np.all(window[0, : 76 - new] == 1.0)  # the buffer began as ones
        assert np.allclose(window[0, 76 - new :], 100 / 10 + 2)  # and the sound was made nearer

    def test_the_word_is_given_the_last_16_embeddings_as_they_are_the_newest_last(self) -> None:
        models = Models(score=lambda value: 0.0)
        models.engine.process(models.frame(fill=100))
        models.engine.process(models.frame(fill=-300))

        features = models.word.asked[-1]

        assert features.shape == (1, 16, 96)
        assert features.dtype == np.float32
        newest, before = features[0, -1], features[0, -2]
        assert np.allclose(newest, models.embedding.asked[-1].mean())
        assert np.allclose(before, models.embedding.asked[-2].mean())

    def test_the_number_of_embeddings_the_word_takes_is_what_the_model_says(self) -> None:
        models = Models(frames=7)

        models.engine.process(models.frame())

        assert models.word.asked[-1].shape == (1, 7, 96)

    @pytest.mark.parametrize("frames", [None, "frames", 0])
    def test_a_model_that_does_not_say_how_many_is_not_one_that_can_be_used(
        self, frames: Any
    ) -> None:
        with pytest.raises(WakeWordError, match="how many embeddings"):
            Models(frames=frames)

    def test_only_what_the_last_ten_seconds_made_is_kept(self) -> None:
        models = Models(score=lambda value: 0.0)

        for _ in range(200):
            models.engine.process(models.frame())

        assert len(models.engine._features) == 120


class TestWhatIsTaken:
    def test_a_score_at_the_threshold_is_the_word_and_one_under_it_is_not(self) -> None:
        for score, word in ((0.5, True), (0.4999, False), (0.5001, True)):
            models = Models(score=score)
            [models.engine.process(models.frame()) for _ in range(5)]

            assert models.engine.process(models.frame()) is word, score

    def test_the_threshold_is_the_one_that_was_given(self) -> None:
        models = Models(score=0.3, threshold=0.25)
        [models.engine.process(models.frame()) for _ in range(5)]

        assert models.engine.process(models.frame())

    def test_with_patience_it_takes_that_many_frames_in_a_row(self) -> None:
        scores = iter([0.9, 0.9, 0.1, 0.9, 0.9, 0.9, 0.9] + [0.9] * 20)
        models = Models(score=lambda value: next(scores), patience=3)
        for _ in range(5):  # the first five are zero whatever they were, and use up the iterator
            models.engine.process(models.frame())

        fired = [models.engine.process(models.frame()) for _ in range(6)]

        # the iterator had given 0.9 0.9 0.1 0.9 0.9 to the first five; then 0.9 0.9 0.9 ...
        assert fired == [False, False, True, True, True, True]

    def test_a_dip_in_the_score_starts_the_count_again(self) -> None:
        scores = iter([0.0] * 5 + [0.9, 0.9, 0.1, 0.9, 0.9, 0.9])
        models = Models(score=lambda value: next(scores), patience=3)
        for _ in range(5):
            models.engine.process(models.frame())

        fired = [models.engine.process(models.frame()) for _ in range(6)]

        assert fired == [False, False, False, False, False, True]

    @pytest.mark.parametrize("threshold", [0, -0.1, 1.01])
    def test_a_threshold_that_is_not_a_score_is_refused(self, threshold: float) -> None:
        with pytest.raises(ValueError, match="threshold"):
            Models(threshold=threshold)

    def test_a_threshold_of_one_is_a_score_that_can_be_asked_for(self) -> None:
        Models(threshold=1.0)

    def test_patience_of_less_than_a_frame_is_refused(self) -> None:
        with pytest.raises(ValueError, match="patience"):
            Models(patience=0)


class TestAReset:
    def test_the_sound_and_the_spectrogram_are_forgotten_and_the_start_is_not_made_again(
        self,
    ) -> None:
        models = Models(score=0.0)
        for _ in range(4):
            models.engine.process(models.frame(fill=900))
        asked = (len(models.melspectrogram.asked), len(models.embedding.asked))

        models.engine.reset()
        models.engine.process(models.frame(fill=-7))

        mel = models.melspectrogram.asked[-1]
        assert mel.shape == (1, FRAME)  # nothing of before comes with it
        assert np.all(mel == -7)
        window = models.embedding.asked[-1]
        assert np.all(window[0, : 76 - rows_of(FRAME)] == 1.0)  # the spectrogram began again
        assert (len(models.melspectrogram.asked), len(models.embedding.asked)) == (
            asked[0] + 1,
            asked[1] + 1,
        )  # one frame, and not four seconds of noise

    def test_the_embeddings_are_those_of_the_start_again(self) -> None:
        models = Models(score=0.0)
        start = [models.engine._features.copy()]
        for _ in range(30):
            models.engine.process(models.frame(fill=500))

        models.engine.reset()

        assert np.array_equal(models.engine._features, start[0])

    def test_the_same_sound_after_a_reset_is_scored_as_it_was_the_first_time(self) -> None:
        models = Models(score=lambda value: float(value.mean()) / 1000)

        def scored() -> list[float]:
            return [
                models.engine.score(models.frame(fill=fill)) for fill in (10, 20, 30, 40, 50, 60)
            ]

        first = scored()
        models.engine.reset()
        second = scored()

        assert first == second
        assert first[:5] == [0.0] * 5
        assert first[5] > 0


class ReadsCounted(Path):
    """A path that says when its content is read."""

    reads: ClassVar[list[Path]] = []

    def read_bytes(self) -> bytes:
        self.reads.append(self)
        return super().read_bytes()


class SpiedModels(WakeWordModels):
    def path_of(self, file: ModelFile) -> Path:
        return ReadsCounted(super().path_of(file))


class TestTheFilesOfTheModels:
    def made(self, directory: Path, content: bytes = b"a model") -> ModelFile:
        file = ModelFile("model.onnx", len(content), hashlib.sha256(content).hexdigest())
        directory.mkdir(parents=True, exist_ok=True)
        (directory / file.name).write_bytes(content)
        return file

    def test_the_catalogue_is_the_four_words_that_the_release_has_a_model_for(self) -> None:
        assert sorted(WAKE_WORDS) == ["alexa", "hey_jarvis", "hey_mycroft", "hey_rhasspy"]

    def test_the_release_is_one_that_does_not_change(self) -> None:
        # The hashes are those of the files of this release: another tag is other files.
        assert RELEASE == "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1"

    def test_every_file_is_known_by_its_size_and_its_hash_and_is_in_the_release(self) -> None:
        for file in (MELSPECTROGRAM, EMBEDDING, *WAKE_WORDS.values()):
            assert file.name.endswith(".onnx")
            assert file.size > 100_000
            assert len(file.sha256) == 64
            assert set(file.sha256) <= set("0123456789abcdef")
            assert file.url == f"{RELEASE}/{file.name}"
            assert file.url.startswith(
                "https://github.com/dscripka/openWakeWord/releases/download/"
            )

    def test_a_word_needs_the_two_models_that_all_share_and_its_own(self) -> None:
        models = WakeWordModels(Path("nowhere"))

        files = models.files_for("hey_jarvis")

        assert files == [MELSPECTROGRAM, EMBEDDING, WAKE_WORDS["hey_jarvis"]]

    def test_a_word_that_there_is_no_model_for_says_which_there_are(self) -> None:
        with pytest.raises(WakeWordError, match=r"no model for 'factotum'.*alexa, hey_jarvis"):
            WakeWordModels(Path("nowhere")).files_for("factotum")

    def test_the_folder_is_the_one_next_to_the_voices_unless_it_is_said(self) -> None:
        assert default_models_directory() == Path.home() / ".cache" / "factotum" / "openwakeword"
        assert WakeWordModels().directory == default_models_directory()
        assert WakeWordModels(Path("x")).directory == Path("x")

    def test_a_file_that_is_there_and_is_what_it_should_be_is_held(self, tmp_path: Path) -> None:
        file = self.made(tmp_path)

        models = WakeWordModels(tmp_path, words={"w": file}, shared=())

        assert models.holds(file)
        assert models.path_of(file) == tmp_path / "model.onnx"
        assert models.missing("w") == []

    def test_one_that_is_not_there_is_missing(self, tmp_path: Path) -> None:
        file = ModelFile("model.onnx", 7, "0" * 64)

        models = WakeWordModels(tmp_path, words={"w": file}, shared=())

        assert not models.holds(file)
        assert models.missing("w") == [file]

    def test_one_with_the_same_size_and_other_content_is_not_what_it_should_be(
        self, tmp_path: Path
    ) -> None:
        file = self.made(tmp_path, b"a model")
        (tmp_path / file.name).write_bytes(b"A MODEL")

        assert not WakeWordModels(tmp_path, words={"w": file}, shared=()).holds(file)

    def test_a_file_of_another_size_is_not_even_read_for_it_may_be_a_very_big_one(
        self, tmp_path: Path
    ) -> None:
        file = self.made(tmp_path, b"a model")
        (tmp_path / file.name).write_bytes(b"a model that is not as long")
        ReadsCounted.reads.clear()

        assert not SpiedModels(tmp_path, words={"w": file}, shared=()).holds(file)

        assert ReadsCounted.reads == []

    def test_one_that_is_the_size_is_read_to_know_what_it_is(self, tmp_path: Path) -> None:
        file = self.made(tmp_path, b"a model")
        ReadsCounted.reads.clear()

        assert SpiedModels(tmp_path, words={"w": file}, shared=()).holds(file)

        assert len(ReadsCounted.reads) == 1

    def test_one_that_is_cut_short_is_not_either(self, tmp_path: Path) -> None:
        file = self.made(tmp_path, b"a model")
        (tmp_path / file.name).write_bytes(b"a mod")

        assert not WakeWordModels(tmp_path, words={"w": file}, shared=()).holds(file)

    def test_what_is_missing_is_only_what_is_missing(self, tmp_path: Path) -> None:
        there = self.made(tmp_path)
        gone = ModelFile("gone.onnx", 3, "1" * 64)

        models = WakeWordModels(tmp_path, words={"w": gone}, shared=(there,))

        assert models.missing("w") == [gone]

    def test_the_command_that_brings_the_models_is_short_for_the_folder_that_nobody_chose(
        self,
    ) -> None:
        assert (
            WakeWordModels().download_command("alexa") == "factotum-device wake-word download alexa"
        )
        assert (
            WakeWordModels(default_models_directory()).download_command("alexa")
            == "factotum-device wake-word download alexa"
        )

    def test_and_names_the_folder_when_it_is_another_quoted_for_the_spaces_in_it(self) -> None:
        folder = Path("a folder") / "models"

        command = WakeWordModels(folder).download_command("hey_jarvis")

        assert command == f'factotum-device wake-word download hey_jarvis --dir "{folder}"'


def models_in(tmp_path: Path) -> tuple[WakeWordModels, list[ModelFile]]:
    """A folder with the three models of a word that is called ``word``, which are three little
    files, each known by its size and its hash as the real ones are."""
    files = []
    for name in ("mel.onnx", "emb.onnx", "word.onnx"):
        content = name.encode()
        files.append(ModelFile(name, len(content), hashlib.sha256(content).hexdigest()))
        (tmp_path / name).write_bytes(content)
    return WakeWordModels(tmp_path, words={"word": files[2]}, shared=files[:2]), files


def fake_loader(loaded: list[Path]) -> Callable[[Path], Any]:
    """What loads a model in place of ``onnxruntime``: the made-up one that its file stands for."""

    def load(path: Path) -> Any:
        loaded.append(path)
        made = Models()
        return {"mel.onnx": made.melspectrogram, "emb.onnx": made.embedding}.get(
            path.name, made.word
        )

    return load


class TestMakingTheEngine:
    def test_it_loads_the_three_models_in_the_order_the_engine_takes_them(
        self, tmp_path: Path
    ) -> None:
        models, _ = models_in(tmp_path)
        loaded: list[Path] = []

        engine = open_wake_word_engine(models, "word", load=fake_loader(loaded))()

        assert [path.name for path in loaded] == ["mel.onnx", "emb.onnx", "word.onnx"]
        assert isinstance(engine, OpenWakeWordEngine)

    def test_nothing_is_loaded_until_it_is_asked_to_make_it(self, tmp_path: Path) -> None:
        models, _ = models_in(tmp_path)
        loaded: list[Path] = []

        open_wake_word_engine(models, "word", load=fake_loader(loaded))

        assert loaded == []

    def test_the_threshold_and_the_patience_that_were_given_are_the_engines(
        self, tmp_path: Path
    ) -> None:
        models, _ = models_in(tmp_path)
        loaded: list[Path] = []
        engine = open_wake_word_engine(
            models, "word", threshold=0.9, patience=2, load=fake_loader(loaded)
        )()

        assert isinstance(engine, OpenWakeWordEngine)
        assert (engine._threshold, engine._patience) == (0.9, 2)

    def test_models_that_are_not_there_are_said_with_what_to_do_about_it(
        self, tmp_path: Path
    ) -> None:
        models, _ = models_in(tmp_path)
        (tmp_path / "emb.onnx").unlink()
        make = open_wake_word_engine(models, "word", load=fake_loader([]))

        with pytest.raises(ModelsNotThereError) as raised:
            make()

        error = raised.value
        assert isinstance(error, WakeWordError)
        assert (error.word, error.directory) == ("word", tmp_path)
        assert [file.name for file in error.missing] == ["emb.onnx"]
        assert error.command == f'factotum-device wake-word download word --dir "{tmp_path}"'
        message = str(error)
        assert "'word'" in message
        assert str(tmp_path) in message
        assert "emb.onnx" in message
        assert "mel.onnx" not in message
        assert f'``factotum-device wake-word download word --dir "{tmp_path}"``' in message

    def test_models_are_not_looked_for_until_the_engine_is_made(self, tmp_path: Path) -> None:
        models, _ = models_in(tmp_path)
        (tmp_path / "emb.onnx").unlink()

        make = open_wake_word_engine(models, "word", load=fake_loader([]))  # nothing is said yet

        with pytest.raises(ModelsNotThereError):
            make()

    def test_nor_is_a_file_read_until_then(self, tmp_path: Path) -> None:
        _, files = models_in(tmp_path)
        spied = SpiedModels(tmp_path, words={"word": files[2]}, shared=files[:2])
        ReadsCounted.reads.clear()

        make = open_wake_word_engine(spied, "word", load=fake_loader([]))

        assert ReadsCounted.reads == []
        make()
        assert len(ReadsCounted.reads) == 3  # the three of them, to know what they are

    def test_a_word_that_has_no_model_is_said_at_once_and_not_when_the_engine_is_made(
        self, tmp_path: Path
    ) -> None:
        models, _ = models_in(tmp_path)

        with pytest.raises(WakeWordError, match="no model for 'jarvis'"):
            open_wake_word_engine(models, "jarvis")


class TestTheSessionOfOnnxruntime:
    def test_it_is_for_the_processor_with_a_thread_and_the_path_it_was_given(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        made: list[Any] = []

        class Options:
            inter_op_num_threads = 0
            intra_op_num_threads = 0

        def session(path: str, *, sess_options: Any, providers: list[str]) -> Any:
            made.append((path, sess_options, providers))
            return "a session"

        fake = SimpleNamespace(SessionOptions=Options, InferenceSession=session)
        monkeypatch.setitem(sys.modules, "onnxruntime", fake)

        loaded = load_session(tmp_path / "model.onnx")

        [(path, options, providers)] = made
        assert cast(Any, loaded) == "a session"
        assert path == str(tmp_path / "model.onnx")
        assert (options.inter_op_num_threads, options.intra_op_num_threads) == (1, 1)
        assert providers == ["CPUExecutionProvider"]


class FakePorcupine:
    """What ``pvporcupine.create`` makes: the index of the keyword that a frame has, or -1."""

    sample_rate = 16_000
    frame_length = 512

    def __init__(self) -> None:
        self.deleted = False

    def process(self, frame: Any) -> int:
        return int(frame[0])

    def delete(self) -> None:
        self.deleted = True


class TestPorcupine:
    def test_the_keyword_and_the_key_are_what_the_engine_is_made_with(self) -> None:
        made: list[dict[str, Any]] = []

        def create(**settings: Any) -> FakePorcupine:
            made.append(settings)
            return FakePorcupine()

        engine = porcupine_engine("a-key", "computer", create=create)()

        assert made == [{"access_key": "a-key", "keywords": ["computer"]}]
        assert isinstance(engine, PorcupineEngine)
        assert (engine.sample_rate, engine.frame_length) == (16_000, 512)

    def test_the_rate_and_the_frame_are_what_porcupine_says_and_not_what_is_assumed(self) -> None:
        class Odd(FakePorcupine):
            sample_rate = 8_000
            frame_length = 256

        engine = PorcupineEngine(Odd())

        assert (engine.sample_rate, engine.frame_length) == (8_000, 256)

    def test_the_keyword_is_jarvis_when_none_is_said(self) -> None:
        made: list[dict[str, Any]] = []

        def create(**settings: Any) -> FakePorcupine:
            made.append(settings)
            return FakePorcupine()

        porcupine_engine("k", create=create)()

        assert made[0]["keywords"] == ["jarvis"]

    def test_a_frame_that_has_a_keyword_in_it_is_the_word_and_one_that_has_none_is_not(
        self,
    ) -> None:
        engine = PorcupineEngine(FakePorcupine())

        assert engine.process(np.array([0])) is True
        assert engine.process(np.array([1])) is True  # any keyword of those that it was made for
        assert engine.process(np.array([-1])) is False

    def test_closing_it_deletes_what_porcupine_holds_and_a_reset_does_nothing(self) -> None:
        porcupine = FakePorcupine()
        engine = PorcupineEngine(porcupine)

        engine.reset()
        assert not porcupine.deleted
        engine.close()

        assert porcupine.deleted

    def test_the_package_is_the_one_that_makes_it_unless_a_test_says_another(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        made: list[dict[str, Any]] = []

        def create(**settings: Any) -> FakePorcupine:
            made.append(settings)
            return FakePorcupine()

        monkeypatch.setitem(sys.modules, "pvporcupine", SimpleNamespace(create=create))

        porcupine_engine("k", "alexa")()

        assert made == [{"access_key": "k", "keywords": ["alexa"]}]

    def test_without_the_package_it_is_said_what_it_is_and_what_does_not_need_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "pvporcupine", None)  # an import that fails

        with pytest.raises(WakeWordError, match=r"not installed.*companies.*openwakeword"):
            porcupine_engine("k")()


class TestWhichEngine:
    def test_the_two_are_named_as_the_settings_name_them(self) -> None:
        assert [engine.value for engine in Engine] == ["openwakeword", "porcupine"]
        assert Engine("porcupine") is Engine.PORCUPINE

    def test_each_listens_for_a_word_that_it_has_a_model_or_a_keyword_for_unless_told_another(
        self,
    ) -> None:
        assert default_word(Engine.OPENWAKEWORD) == "hey_jarvis"
        assert default_word(Engine.OPENWAKEWORD) in WAKE_WORDS
        assert default_word(Engine.PORCUPINE) == "jarvis"


class TestChoosingTheEngine:
    """``wake_word_engine``: the one place that knows which engine takes what, for every client."""

    def test_openwakeword_is_made_of_its_models_with_the_threshold_and_the_patience_it_was_given(
        self, tmp_path: Path
    ) -> None:
        models, _ = models_in(tmp_path)
        loaded: list[Path] = []

        engine = wake_word_engine(
            Engine.OPENWAKEWORD,
            "word",
            models=models,
            threshold=0.8,
            patience=3,
            load=fake_loader(loaded),
        )()

        assert isinstance(engine, OpenWakeWordEngine)
        assert (engine._threshold, engine._patience) == (0.8, 3)
        assert [path.name for path in loaded] == ["mel.onnx", "emb.onnx", "word.onnx"]

    def test_it_is_the_score_of_the_package_and_one_frame_unless_it_is_said(
        self, tmp_path: Path
    ) -> None:
        models, _ = models_in(tmp_path)

        engine = wake_word_engine(
            Engine.OPENWAKEWORD, "word", models=models, load=fake_loader([])
        )()

        assert isinstance(engine, OpenWakeWordEngine)
        assert (engine._threshold, engine._patience) == (0.5, 1)

    def test_the_models_are_in_the_folder_that_nobody_chose_unless_a_folder_is_given(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        with pytest.raises(WakeWordError) as raised:
            wake_word_engine(Engine.OPENWAKEWORD, "hey_jarvis")()

        assert str(tmp_path / ".cache" / "factotum" / "openwakeword") in str(raised.value)

    def test_a_word_that_has_no_model_is_said_at_once(self) -> None:
        with pytest.raises(WakeWordError, match="no model for 'jarvis'"):
            wake_word_engine(Engine.OPENWAKEWORD, "jarvis")

    def test_porcupine_is_made_with_its_key_and_its_word(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        made: list[dict[str, Any]] = []

        def create(**settings: Any) -> FakePorcupine:
            made.append(settings)
            return FakePorcupine()

        monkeypatch.setitem(sys.modules, "pvporcupine", SimpleNamespace(create=create))

        engine = wake_word_engine(Engine.PORCUPINE, "computer", access_key="a-key")()

        assert isinstance(engine, PorcupineEngine)
        assert made == [{"access_key": "a-key", "keywords": ["computer"]}]

    def test_porcupine_with_no_key_is_refused_at_once_and_says_who_has_keys_and_what_needs_none(
        self,
    ) -> None:
        with pytest.raises(
            WakeWordError,
            match=r"access key \(\$PICOVOICE_ACCESS_KEY\).*companies.*openwakeword needs none",
        ):
            wake_word_engine(Engine.PORCUPINE, "jarvis")

    def test_a_key_that_openwakeword_does_not_need_is_not_used(self, tmp_path: Path) -> None:
        models, _ = models_in(tmp_path)

        engine = wake_word_engine(
            Engine.OPENWAKEWORD,
            "word",
            access_key="a-key",
            models=models,
            load=fake_loader([]),
        )()

        assert isinstance(engine, OpenWakeWordEngine)


class TestTheMadeUpModelsAreAsAStrictAsTheRealOnes:
    """A fake that is looser than the real thing hides what the real thing does: a model of
    onnxruntime is asked for its outputs by the name of its input, and these refuse the rest."""

    def test_a_model_is_asked_by_the_name_of_its_input(self) -> None:
        session = Models().melspectrogram

        with pytest.raises(AssertionError, match="by the name of its input"):
            session.run(None, {"another": np.zeros((1, 1_760), dtype=np.float32)})

    def test_and_for_the_type_that_it_was_made_for(self) -> None:
        session = Models().melspectrogram

        with pytest.raises(AssertionError, match="32-bit floats"):
            session.run(None, {"input": np.zeros((1, 1_760), dtype=np.int16)})

    def test_and_for_all_of_its_outputs(self) -> None:
        session = Models().melspectrogram

        with pytest.raises(AssertionError, match="all its outputs"):
            session.run(["output"], {"input": np.zeros((1, 1_760), dtype=np.float32)})
