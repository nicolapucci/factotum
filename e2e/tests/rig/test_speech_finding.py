"""Where the real models are looked for, what is done to run them on the graphics card, and what the
recorders under the real engines remember: nothing here loads a model or touches a card, and nothing
is downloaded (a model that is not found is a skip, never a fetch)."""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from factotum.e2e.speech import (
    CUDA_VARIABLE,
    DEVICE_VARIABLE,
    VOICE_VARIABLE,
    WHISPER_VARIABLE,
    RealSpeech,
    RecordedPiper,
    RecordedWhisper,
    ensure_cuda_libraries,
    find_speech,
)
from factotum.model_gateway.providers.base import SpeechPiece

SNAPSHOTS = ".cache/huggingface/hub/models--Systran--faster-whisper-small/snapshots"
MEDIUM = ".cache/huggingface/hub/models--Systran--faster-whisper-medium/snapshots"
LARGE = ".cache/huggingface/hub/models--Systran--faster-whisper-large-v3/snapshots"
PIPER = ".cache/factotum/piper"


def a_whisper_model(folder: Path) -> Path:
    folder.mkdir(parents=True)
    for name in ("model.bin", "config.json", "tokenizer.json"):
        (folder / name).write_bytes(b"x")
    return folder


def a_voice(folder: Path, name: str = "it_IT-paola-medium.onnx") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    voice = folder / name
    voice.write_bytes(b"x")
    (folder / f"{name}.json").write_text("{}", encoding="utf-8")
    return voice


class TestFindingTheModels:
    def test_the_models_in_the_caches_are_found(self, tmp_path: Path) -> None:
        snapshot = a_whisper_model(tmp_path / SNAPSHOTS / "abc")
        voice = a_voice(tmp_path / PIPER)

        assert find_speech({}, tmp_path) == RealSpeech(whisper=snapshot, voice=voice)

    def test_the_newest_snapshot_is_the_one_used(self, tmp_path: Path) -> None:
        older = a_whisper_model(tmp_path / SNAPSHOTS / "older")
        newer = a_whisper_model(tmp_path / SNAPSHOTS / "newer")
        os.utime(older, (1_000_000_000, 1_000_000_000))
        os.utime(newer, (2_000_000_000, 2_000_000_000))
        a_voice(tmp_path / PIPER)

        found = find_speech({}, tmp_path)

        assert found is not None
        assert found.whisper == newer

    def test_the_bigger_model_is_the_one_used_when_there_are_two(self, tmp_path: Path) -> None:
        a_whisper_model(tmp_path / SNAPSHOTS / "small")
        medium = a_whisper_model(tmp_path / MEDIUM / "medium")
        os.utime(medium, (1_000_000_000, 1_000_000_000))  # however old it is: bigger hears better
        a_voice(tmp_path / PIPER)

        found = find_speech({}, tmp_path)

        assert found is not None
        assert found.whisper == medium

    def test_the_biggest_of_three_is_the_one_used(self, tmp_path: Path) -> None:
        a_whisper_model(tmp_path / SNAPSHOTS / "small")
        a_whisper_model(tmp_path / MEDIUM / "medium")
        large = a_whisper_model(tmp_path / LARGE / "large")
        a_voice(tmp_path / PIPER)

        found = find_speech({}, tmp_path)

        assert found is not None
        assert found.whisper == large

    def test_the_smaller_model_is_used_when_the_bigger_is_not_there_or_is_not_whole(
        self, tmp_path: Path
    ) -> None:
        small = a_whisper_model(tmp_path / SNAPSHOTS / "small")
        half = a_whisper_model(tmp_path / MEDIUM / "half")
        (half / "model.bin").unlink()  # a download that was not finished is not a model
        a_voice(tmp_path / PIPER)

        found = find_speech({}, tmp_path)

        assert found is not None
        assert found.whisper == small

    def test_a_snapshot_without_the_model_in_it_is_not_a_model(self, tmp_path: Path) -> None:
        snapshot = a_whisper_model(tmp_path / SNAPSHOTS / "abc")
        (snapshot / "model.bin").unlink()
        a_voice(tmp_path / PIPER)

        assert find_speech({}, tmp_path) is None

    def test_a_voice_without_its_description_is_not_found(self, tmp_path: Path) -> None:
        a_whisper_model(tmp_path / SNAPSHOTS / "abc")
        voice = a_voice(tmp_path / PIPER)
        voice.with_name(f"{voice.name}.json").unlink()

        assert find_speech({}, tmp_path) is None

    def test_nothing_on_the_machine_is_nothing_and_not_a_download(self, tmp_path: Path) -> None:
        assert find_speech({}, tmp_path) is None

    def test_a_folder_in_the_variable_is_used_instead_of_the_cache(self, tmp_path: Path) -> None:
        a_whisper_model(tmp_path / SNAPSHOTS / "abc")
        a_voice(tmp_path / PIPER)
        mine = a_whisper_model(tmp_path / "mine")

        found = find_speech({WHISPER_VARIABLE: str(mine)}, tmp_path)

        assert found is not None
        assert found.whisper == mine

    def test_a_name_in_the_variable_is_not_a_folder_and_is_never_fetched(
        self, tmp_path: Path
    ) -> None:
        a_whisper_model(tmp_path / SNAPSHOTS / "abc")
        a_voice(tmp_path / PIPER)

        assert find_speech({WHISPER_VARIABLE: "small"}, tmp_path) is None

    def test_a_file_in_the_variable_of_the_voice_is_used_instead_of_the_cache(
        self, tmp_path: Path
    ) -> None:
        a_whisper_model(tmp_path / SNAPSHOTS / "abc")
        mine = a_voice(tmp_path / "voices", "en_US-lessac-medium.onnx")

        found = find_speech({VOICE_VARIABLE: str(mine)}, tmp_path)

        assert found is not None
        assert found.voice == mine

    def test_a_name_in_the_variable_of_the_voice_is_not_a_file_and_is_never_fetched(
        self, tmp_path: Path
    ) -> None:
        a_whisper_model(tmp_path / SNAPSHOTS / "abc")
        a_voice(tmp_path / PIPER)

        assert find_speech({VOICE_VARIABLE: "it_IT-paola-medium"}, tmp_path) is None

    def test_whisper_runs_on_the_processor_unless_the_variable_says_the_card(
        self, tmp_path: Path
    ) -> None:
        a_whisper_model(tmp_path / SNAPSHOTS / "abc")
        a_voice(tmp_path / PIPER)

        on_the_processor = find_speech({}, tmp_path)
        on_the_card = find_speech({DEVICE_VARIABLE: "cuda"}, tmp_path)

        assert on_the_processor is not None
        assert on_the_card is not None
        assert (on_the_processor.device, on_the_card.device) == ("cpu", "cuda")

    def test_the_house_is_spoken_to_in_italian(self, tmp_path: Path) -> None:
        a_whisper_model(tmp_path / SNAPSHOTS / "abc")
        a_voice(tmp_path / PIPER)

        found = find_speech({}, tmp_path)

        assert found is not None
        assert found.languages == ("it",)


def a_speech(device: str) -> RealSpeech:
    return RealSpeech(whisper=Path("whisper"), voice=Path("voice.onnx"), device=device)


def a_folder_with_cublas(folder: Path) -> Path:
    folder.mkdir(parents=True)
    (folder / "cublas64_12.dll").write_bytes(b"x")
    return folder


class TestTheCudaLibraries:
    def test_nothing_is_done_where_the_card_is_not_used(self, tmp_path: Path) -> None:
        environ = {CUDA_VARIABLE: str(a_folder_with_cublas(tmp_path / "cuda")), "PATH": "bin"}
        registered: list[str] = []

        found = ensure_cuda_libraries(a_speech("cpu"), environ, registered.append)

        assert (found, environ["PATH"], registered) == (None, "bin", [])

    def test_the_folder_of_the_variable_is_put_on_the_path_and_made_known_to_windows(
        self, tmp_path: Path
    ) -> None:
        folder = a_folder_with_cublas(tmp_path / "cuda")
        environ = {CUDA_VARIABLE: str(folder), "PATH": "bin"}
        registered: list[str] = []

        found = ensure_cuda_libraries(a_speech("cuda"), environ, registered.append)

        assert found == folder
        assert environ["PATH"] == f"{folder}{os.pathsep}bin"
        assert registered == [str(folder)]

    def test_the_folder_where_ollama_keeps_its_own_is_found_without_being_told(
        self, tmp_path: Path
    ) -> None:
        folder = a_folder_with_cublas(tmp_path / "Programs/Ollama/lib/ollama/cuda_v12")
        environ = {"LOCALAPPDATA": str(tmp_path), "PATH": "bin"}

        found = ensure_cuda_libraries(a_speech("cuda"), environ, lambda _folder: None)

        assert found == folder
        assert environ["PATH"].startswith(str(folder))

    def test_a_folder_with_no_cublas_in_it_is_not_used(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        environ = {CUDA_VARIABLE: str(empty), "PATH": "bin"}
        registered: list[str] = []

        found = ensure_cuda_libraries(a_speech("cuda"), environ, registered.append)

        assert (found, environ["PATH"], registered) == (None, "bin", [])


class FakeWhisper:
    def __init__(self) -> None:
        self.asked: list[tuple[bytes, tuple[str, ...] | None]] = []

    def transcribe_wav(self, wav_bytes: bytes, languages: tuple[str, ...] | None) -> str:
        self.asked.append((wav_bytes, languages))
        return "ciao"


class FakePiper:
    def synthesize_wav(self, text: str) -> bytes:
        return text.encode()

    def synthesize_pieces(self, text: str) -> Iterator[SpeechPiece]:
        return iter([SpeechPiece(sample_rate=22_050, pcm=text.encode())])


class TestTheRecordersUnderTheRealEngines:
    def test_what_the_engine_of_whisper_was_asked_and_what_it_heard_is_remembered(self) -> None:
        engine = FakeWhisper()
        recorded = RecordedWhisper(engine)

        heard = recorded.build("small").transcribe_wav(b"a recording", ("it",))

        assert heard == "ciao"
        assert (recorded.models, recorded.heard, recorded.languages) == (
            ["small"],
            ["ciao"],
            [("it",)],
        )
        assert engine.asked == [(b"a recording", ("it",))]

    def test_a_whisper_that_is_broken_falls_over_and_remembers_nothing(self) -> None:
        engine = FakeWhisper()
        recorded = RecordedWhisper(engine)
        recorded.broken = True

        with pytest.raises(RuntimeError, match="fell over"):
            recorded.transcribe_wav(b"a recording", None)

        assert (recorded.heard, engine.asked) == ([], [])

    def test_what_the_engine_of_piper_was_asked_to_say_is_remembered(self) -> None:
        recorded = RecordedPiper(FakePiper())

        clip = recorded.build("it_IT-paola-medium").synthesize_wav("Buongiorno.")

        assert clip == b"Buongiorno."
        assert (recorded.voices, recorded.spoken) == (["it_IT-paola-medium"], ["Buongiorno."])

    def test_what_the_voice_was_asked_to_say_in_pieces_is_remembered_too(self) -> None:
        recorded = RecordedPiper(FakePiper())

        [piece] = recorded.build("it_IT-paola-medium").synthesize_pieces("Buongiorno.")

        assert piece == SpeechPiece(sample_rate=22_050, pcm=b"Buongiorno.")
        assert (recorded.voices, recorded.spoken) == (["it_IT-paola-medium"], ["Buongiorno."])
