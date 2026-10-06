"""Having the models of a wake word: what is fetched, from where, what is kept and what is not. The
server is a test's (``httpx.MockTransport``) and the models are two little files, known by their
size and their hash as the real ones are, so that what is checked is the downloader and not the
network or the models of someone else."""

import asyncio
import errno
import hashlib
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st

from factotum.client_audio import ModelFile, WakeWordError, WakeWordModels
from factotum.device.wakewords import TIMEOUT, DownloadError, download, size_of

SPECTROGRAM = b"the spectrogram"
WORD = b"the model of the word"


def known(name: str, content: bytes) -> ModelFile:
    return ModelFile(name, len(content), hashlib.sha256(content).hexdigest())


def models_in(directory: Path) -> WakeWordModels:
    return WakeWordModels(
        directory,
        words={"word": known("word.onnx", WORD)},
        shared=(known("mel.onnx", SPECTROGRAM),),
    )


class Server:
    """What serves the files of a release: each at its own address, and what it does not have is
    not found. What was asked for is kept."""

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files = {"mel.onnx": SPECTROGRAM, "word.onnx": WORD} if files is None else files
        self.asked: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(str(request.url))
        name = request.url.path.rsplit("/", 1)[-1]
        if name not in self.files:
            return httpx.Response(404)
        return httpx.Response(200, content=self.files[name])

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


async def fetched(
    models: WakeWordModels, server: Server | None = None, word: str = "word"
) -> tuple[list[ModelFile], list[str]]:
    said: list[str] = []
    async with (server or Server()).client() as client:
        files = await download(models, word, client=client, say=said.append)
    return files, said


class TestWhatIsFetched:
    async def test_the_files_that_are_not_there_are_fetched_and_kept(self, tmp_path: Path) -> None:
        models = models_in(tmp_path / "models")

        files, _ = await fetched(models)

        assert [file.name for file in files] == ["mel.onnx", "word.onnx"]
        assert (tmp_path / "models" / "mel.onnx").read_bytes() == SPECTROGRAM
        assert (tmp_path / "models" / "word.onnx").read_bytes() == WORD
        assert models.missing("word") == []

    async def test_they_are_fetched_from_where_the_release_has_them(self, tmp_path: Path) -> None:
        server = Server()

        await fetched(models_in(tmp_path), server)

        assert server.asked == [
            "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/mel.onnx",
            "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/word.onnx",
        ]

    async def test_each_is_said_as_it_is_fetched_with_how_big_and_from_where(
        self, tmp_path: Path
    ) -> None:
        _, said = await fetched(models_in(tmp_path))

        assert said == [
            "mel.onnx (0.0 kB) from "
            "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/mel.onnx",
            "word.onnx (0.0 kB) from "
            "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/word.onnx",
        ]

    async def test_what_is_there_already_is_left_as_it_is_and_not_asked_for(
        self, tmp_path: Path
    ) -> None:
        models = models_in(tmp_path)
        await fetched(models)
        server = Server()

        files, said = await fetched(models, server)

        assert files == []
        assert server.asked == []
        assert said == ["mel.onnx: there already", "word.onnx: there already"]

    async def test_only_what_is_missing_is_fetched(self, tmp_path: Path) -> None:
        models = models_in(tmp_path)
        (tmp_path / "mel.onnx").write_bytes(SPECTROGRAM)

        files, said = await fetched(models)

        assert [file.name for file in files] == ["word.onnx"]
        assert said[0] == "mel.onnx: there already"

    @pytest.mark.parametrize(
        "found", [b"the spec", b"THE SPECTROGRAM", b""], ids=["cut short", "another file", "empty"]
    )
    async def test_a_file_that_is_there_but_is_not_what_it_should_be_is_fetched_again(
        self, tmp_path: Path, found: bytes
    ) -> None:
        models = models_in(tmp_path)
        (tmp_path / "mel.onnx").write_bytes(found)

        files, _ = await fetched(models)

        assert "mel.onnx" in [file.name for file in files]
        assert (tmp_path / "mel.onnx").read_bytes() == SPECTROGRAM

    async def test_the_folder_is_made_when_it_is_not_there(self, tmp_path: Path) -> None:
        folder = tmp_path / "a" / "b" / "models"

        await fetched(models_in(folder))

        assert (folder / "mel.onnx").exists()

    async def test_a_redirect_is_followed_as_github_makes_one_to_where_the_file_is(
        self, tmp_path: Path
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            name = request.url.path.rsplit("/", 1)[-1]
            if request.url.host == "github.com":
                return httpx.Response(302, headers={"location": f"https://objects.example/{name}"})
            return httpx.Response(200, content={"mel.onnx": SPECTROGRAM, "word.onnx": WORD}[name])

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await download(models_in(tmp_path), "word", client=client)

        assert (tmp_path / "mel.onnx").read_bytes() == SPECTROGRAM

    async def test_a_word_that_there_is_no_model_for_is_said_before_anything_is_fetched(
        self, tmp_path: Path
    ) -> None:
        server = Server()

        with pytest.raises(WakeWordError, match="no model for 'other'"):
            await fetched(models_in(tmp_path), server, word="other")

        assert server.asked == []


class TestWhatIsNotKept:
    async def test_a_file_of_another_content_is_refused_and_nothing_is_kept(
        self, tmp_path: Path
    ) -> None:
        server = Server({"mel.onnx": SPECTROGRAM, "word.onnx": b"another model, of the same ..."})

        with pytest.raises(DownloadError, match=r"word\.onnx: what came is not the file"):
            await fetched(models_in(tmp_path), server)

        assert not (tmp_path / "word.onnx").exists()
        assert not (tmp_path / "word.onnx.part").exists()

    async def test_a_file_of_the_size_but_another_content_is_refused_too(
        self, tmp_path: Path
    ) -> None:
        server = Server({"mel.onnx": b"X" * len(SPECTROGRAM), "word.onnx": WORD})

        with pytest.raises(DownloadError, match=r"mel\.onnx: what came is not the file"):
            await fetched(models_in(tmp_path), server)

        assert not (tmp_path / "mel.onnx").exists()

    async def test_what_was_fetched_before_the_one_that_failed_is_kept(
        self, tmp_path: Path
    ) -> None:
        server = Server({"mel.onnx": SPECTROGRAM, "word.onnx": b"no"})

        with pytest.raises(DownloadError):
            await fetched(models_in(tmp_path), server)

        assert (
            tmp_path / "mel.onnx"
        ).read_bytes() == SPECTROGRAM  # it is the file that it should be

    async def test_a_server_that_says_it_has_not_got_it_is_said_with_what_it_answered(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(DownloadError, match=r"mel\.onnx: https://.*/mel\.onnx answered 404"):
            await fetched(models_in(tmp_path), Server({}))

    async def test_a_server_that_cannot_be_reached_is_said_without_what_it_said(
        self, tmp_path: Path
    ) -> None:
        def unreachable(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("[Errno 11001] getaddrinfo failed: token=hunter2")

        async with httpx.AsyncClient(transport=httpx.MockTransport(unreachable)) as client:
            with pytest.raises(DownloadError) as raised:
                await download(models_in(tmp_path), "word", client=client)

        assert "mel.onnx: could not be fetched from https://" in str(raised.value)
        assert "ConnectError" in str(raised.value)
        assert "hunter2" not in str(raised.value)  # what a library says is not repeated
        assert raised.value.__cause__ is None

    async def test_a_file_that_is_followed_by_more_is_not_the_file(self, tmp_path: Path) -> None:
        class TwoChunks(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                yield SPECTROGRAM
                yield b"and something more"

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("mel.onnx"):
                return httpx.Response(200, stream=TwoChunks())
            return httpx.Response(200, content=WORD)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(DownloadError, match=r"mel\.onnx: what came is not the file"):
                await download(models_in(tmp_path), "word", client=client)

        assert not (tmp_path / "mel.onnx").exists()

    async def test_a_file_that_cannot_be_written_is_said_and_nothing_half_written_is_left(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        written = Path.write_bytes

        def fails_halfway(self: Path, data: bytes) -> int:
            written(self, data[: len(data) // 2])
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(Path, "write_bytes", fails_halfway)

        with pytest.raises(DownloadError) as raised:
            await fetched(models_in(tmp_path))

        assert str(raised.value).startswith("mel.onnx: could not be kept in ")
        assert str(raised.value).endswith("(No space left on device)")
        assert str(tmp_path) in str(raised.value)
        assert not (tmp_path / "mel.onnx").exists()
        assert not (tmp_path / "mel.onnx.part").exists()

    async def test_a_folder_that_cannot_be_made_is_said_too(self, tmp_path: Path) -> None:
        (tmp_path / "models").write_bytes(b"this is a file, and not a folder")

        with pytest.raises(DownloadError, match=r"mel\.onnx: could not be kept in "):
            await fetched(models_in(tmp_path / "models"))

    async def test_a_body_that_is_bigger_than_the_file_is_not_read_to_the_end(
        self, tmp_path: Path
    ) -> None:
        pulled = 0

        class Endless(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                nonlocal pulled
                while True:
                    pulled += 1
                    yield b"x" * 1_000

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=Endless())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(DownloadError, match="what came is not the file"):
                await download(models_in(tmp_path), "word", client=client)

        assert pulled == 1  # the first chunk was already more than the file is
        assert not (tmp_path / "mel.onnx").exists()
        assert not (tmp_path / "mel.onnx.part").exists()

    @given(body=st.binary(max_size=64))
    def test_nothing_that_is_not_the_file_is_ever_kept_whatever_it_is(self, body: bytes) -> None:
        if body == SPECTROGRAM:
            return  # that one is the file

        async def attempt(folder: Path) -> None:
            with pytest.raises(DownloadError):
                await fetched(models_in(folder), Server({"mel.onnx": body, "word.onnx": WORD}))

        with tempfile.TemporaryDirectory() as folder:
            asyncio.run(attempt(Path(folder)))
            assert not list(Path(folder).glob("*"))


class TestHowLongItWaits:
    def test_a_server_that_does_not_answer_is_given_up_on(self) -> None:
        waits = (TIMEOUT.connect, TIMEOUT.read, TIMEOUT.write, TIMEOUT.pool)

        assert all(wait is not None and 0 < wait <= 120 for wait in waits)
        assert TIMEOUT.connect == 10.0  # an address that is not there is known sooner


class TestHowBigAFileIs:
    @pytest.mark.parametrize(
        ("size", "said"),
        [
            (0, "0.0 kB"),
            (99_999, "100.0 kB"),
            (100_000, "0.1 MB"),
            (1_087_958, "1.1 MB"),
            (1_326_578, "1.3 MB"),
        ],
    )
    def test_it_is_in_the_unit_that_a_person_reads(self, size: int, said: str) -> None:
        assert size_of(size) == said
