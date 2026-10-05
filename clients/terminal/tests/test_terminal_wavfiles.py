"""Audio in files: the client that tests the whole stack with no hardware says what is in a WAV file
as a microphone would, and writes what the assistant says to another."""

import asyncio
import io
import wave
from pathlib import Path

import pytest

from factotum.contracts.audio import AudioPayload, wav_of
from factotum.terminal.audio import AudioStream
from factotum.terminal.wavfiles import WavFileInput, WavFileOutput


def wav_file(path: Path, pcm: bytes, rate: int = 16000) -> Path:
    path.write_bytes(wav_of(pcm, rate).data)
    return path


def stereo_file(path: Path) -> Path:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_writer:
        wav_writer.setnchannels(2)
        wav_writer.setsampwidth(2)
        wav_writer.setframerate(16000)
        wav_writer.writeframes(bytes(8))
    path.write_bytes(buffer.getvalue())
    return path


async def said(source: WavFileInput) -> list[tuple[int, list[bytes]]]:
    """What a person said, as the rate and the chunks of each utterance."""
    return [
        (stream.sample_rate, [chunk async for chunk in stream.chunks])
        async for stream in source.listen()
    ]


class TestRecordingsInFiles:
    async def test_each_file_is_an_utterance_said_in_the_order_given(self, tmp_path: Path) -> None:
        first = wav_file(tmp_path / "first.wav", b"\x01\x00" * 4000, 16000)
        second = wav_file(tmp_path / "second.wav", b"\x02\x00" * 100, 22050)

        utterances = await said(WavFileInput(first, second))

        assert [(rate, b"".join(chunks)) for rate, chunks in utterances] == [
            (16000, b"\x01\x00" * 4000),
            (22050, b"\x02\x00" * 100),
        ]

    async def test_they_come_in_chunks_as_a_microphone_would_hand_them_over(
        self, tmp_path: Path
    ) -> None:
        path = wav_file(tmp_path / "question.wav", bytes(3200 * 2 + 100))

        [(_, chunks)] = await said(WavFileInput(path))
        [(_, finer)] = await said(WavFileInput(path, chunk_ms=50))

        assert [len(chunk) for chunk in chunks] == [3200, 3200, 100]
        assert [len(chunk) for chunk in finer] == [1600] * 4 + [100]

    async def test_what_is_streamed_is_an_audio_stream(self, tmp_path: Path) -> None:
        path = wav_file(tmp_path / "question.wav", bytes(64))

        [stream] = [s async for s in WavFileInput(path).listen()]

        assert isinstance(stream, AudioStream)
        await stream.chunks.aclose()

    async def test_at_the_pace_of_a_microphone_when_it_is_asked_to(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        slept: list[float] = []

        async def sleep(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", sleep)
        path = wav_file(tmp_path / "question.wav", bytes(3200 * 2))

        await said(WavFileInput(path, realtime=True))
        assert slept == pytest.approx([0.1, 0.1])
        slept.clear()
        await said(WavFileInput(path))
        assert slept == []

    async def test_when_the_last_is_said_there_is_no_more(self, tmp_path: Path) -> None:
        path = wav_file(tmp_path / "question.wav", bytes(64))

        assert len(await said(WavFileInput(path))) == 1
        assert await said(WavFileInput()) == []

    async def test_they_can_be_said_again(self, tmp_path: Path) -> None:
        source = WavFileInput(wav_file(tmp_path / "question.wav", b"\x01\x00" * 10))

        assert await said(source) == await said(source)

    def test_a_file_that_is_not_there_is_known_when_it_is_asked_for(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            WavFileInput(tmp_path / "nothing.wav")

    @pytest.mark.parametrize(
        ("what", "reason"),
        [("not-audio.wav", "not a WAV file"), ("stereo.wav", "16-bit mono")],
    )
    def test_a_file_that_is_not_what_a_stream_carries_says_which_and_why(
        self, tmp_path: Path, what: str, reason: str
    ) -> None:
        path = tmp_path / what
        if what == "stereo.wav":
            stereo_file(path)
        else:
            path.write_bytes(b"this is not a recording")

        with pytest.raises(ValueError, match=reason) as refused:
            WavFileInput(path)

        assert str(path) in str(refused.value)


def clip(pcm: bytes, rate: int = 22050) -> AudioPayload:
    return wav_of(pcm, rate)


def read(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as wav_reader:
        return wav_reader.readframes(wav_reader.getnframes()), wav_reader.getframerate()


class TestWhatIsSaidInAFile:
    async def test_what_is_played_is_written_to_the_file(self, tmp_path: Path) -> None:
        output = WavFileOutput(tmp_path / "answer.wav")

        await output.play(clip(b"\x01\x00" * 22050))

        assert read(output.path) == (b"\x01\x00" * 22050, 22050)
        assert output.seconds == pytest.approx(1.0)

    async def test_the_clips_of_an_answer_follow_each_other_and_the_file_is_whole_after_each(
        self, tmp_path: Path
    ) -> None:
        output = WavFileOutput(tmp_path / "answer.wav")

        await output.play(clip(b"\x01\x00" * 100))
        assert read(output.path) == (b"\x01\x00" * 100, 22050)
        await output.play(clip(b"\x02\x00" * 50))

        assert read(output.path) == (b"\x01\x00" * 100 + b"\x02\x00" * 50, 22050)
        assert output.seconds == pytest.approx(150 / 22050)

    async def test_nothing_said_is_no_file_and_no_seconds(self, tmp_path: Path) -> None:
        output = WavFileOutput(tmp_path / "answer.wav")

        assert output.seconds == 0.0
        assert not output.path.exists()

    async def test_a_file_has_one_rate_and_a_clip_at_another_is_refused_and_leaves_it_as_it_was(
        self, tmp_path: Path
    ) -> None:
        output = WavFileOutput(tmp_path / "answer.wav")
        await output.play(clip(b"\x01\x00" * 100, 22050))

        with pytest.raises(ValueError, match="one rate"):
            await output.play(clip(b"\x02\x00" * 100, 16000))

        assert read(output.path) == (b"\x01\x00" * 100, 22050)

    async def test_what_is_not_a_wav_file_is_not_played(self, tmp_path: Path) -> None:
        output = WavFileOutput(tmp_path / "answer.wav")

        with pytest.raises(ValueError, match="not a WAV file"):
            await output.play(AudioPayload(sample_rate=16000, data=b"spoken-aloud"))

        assert not output.path.exists()
