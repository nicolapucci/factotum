import asyncio
import io
import sys
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from factotum.contracts.audio import AudioPayload, wav_of
from factotum.contracts.cards import Card
from factotum.contracts.commands import SetVolume, ShowCard
from factotum.contracts.edge import (
    PROTOCOL_VERSION,
    CommandFrame,
    ErrorFrame,
    Heard,
    PartFrame,
    Ready,
    TurnEnded,
    TurnStatus,
)
from factotum.contracts.ids import DeviceId
from factotum.contracts.response import Speech
from factotum.terminal import cli
from factotum.terminal.audio import DEFAULT_WAKE_WORD
from factotum.terminal.cli import (
    DEFAULT_URL,
    HELP,
    TOKEN_VARIABLE,
    Options,
    parse_args,
    run,
    stdin_lines,
)
from factotum.terminal.connection import Connection, CouldNotConnect
from factotum.terminal.profiles import get_profile
from factotum.terminal.testing import (
    FakeVoiceInput,
    FakeVoiceOutput,
    MemoryConnection,
    Typed,
    never,
)

RECORDING = AudioPayload(sample_rate=16000, data=b"raw-microphone-bytes")

CONNECTED = "--       connected as the test device"
INVITATION = "--       type what you would say (/help)"


def connected(profile: str) -> MemoryConnection:
    """A connection to an edge that has already said it is ready for a device of this profile."""
    connection = MemoryConnection()
    connection.say(
        Ready(
            protocol_version=PROTOCOL_VERSION,
            session_id="s1",
            device_id=DeviceId(uuid4()),
            name="the test device",
            capabilities=get_profile(profile).capabilities,
        )
    )
    return connection


async def opened(connection: MemoryConnection) -> Connection:
    return connection


async def session(
    profile: str,
    *lines: str | Callable[[], Any],
    connection: MemoryConnection | None = None,
) -> tuple[int, list[str], MemoryConnection]:
    """A person at a terminal of this profile who types these lines. What comes out is returned as
    the lines of the screen, without the notices of a connection that starts."""
    connection = connection or connected(profile)
    out = io.StringIO()
    code = await run(
        Options(url=DEFAULT_URL, profile=get_profile(profile), hardware_id=None, token=None),
        lines=Typed(*lines).read(),
        out=out,
        open_connection=lambda _url: opened(connection),
    )
    return code, out.getvalue().splitlines(), connection


def utterance_ended(connection: MemoryConnection, identity: str = "u1") -> Callable[[], None]:
    def end() -> None:
        connection.say(TurnEnded(utterance_id=identity, status=TurnStatus.FINISHED))

    return end


class TestTheArguments:
    def test_by_default_it_is_a_speaker_at_the_edge_of_a_development_machine(self) -> None:
        options = parse_args([], {})

        assert options.url == DEFAULT_URL
        assert options.profile.name == "speaker"
        assert (options.hardware_id, options.token) == (None, None)

    def test_a_profile_and_an_address_can_be_chosen(self) -> None:
        options = parse_args(["--profile", "screen", "--url", "ws://edge.lan:8084/v1/connect"], {})

        assert options.profile.name == "screen"
        assert options.url == "ws://edge.lan:8084/v1/connect"

    def test_the_credential_comes_from_the_command_line_or_else_the_environment(self) -> None:
        assert parse_args([], {TOKEN_VARIABLE: "from-env"}).token == "from-env"
        assert (
            parse_args(["--token", "from-flag"], {TOKEN_VARIABLE: "from-env"}).token == "from-flag"
        )

    def test_another_device_can_be_presented(self) -> None:
        assert parse_args(["--hardware-id", "lab-9"], {}).hardware_id == "lab-9"

    def test_by_default_there_is_no_voice_and_the_wake_word_is_the_usual_one(self) -> None:
        options = parse_args([], {})

        assert options.voice is False
        assert options.wake_word == DEFAULT_WAKE_WORD

    def test_voice_and_a_wake_word_can_be_chosen(self) -> None:
        options = parse_args(["--voice", "--wake-word", "alexa"], {})

        assert options.voice is True
        assert options.wake_word == "alexa"

    def test_the_access_key_comes_from_the_command_line_or_else_the_environment(self) -> None:
        env = {"PICOVOICE_ACCESS_KEY": "from-env"}
        assert parse_args([], env).access_key == "from-env"
        assert parse_args(["--access-key", "from-flag"], env).access_key == "from-flag"

    def test_a_profile_that_does_not_exist_is_refused_and_the_profiles_are_listed(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as stopped:
            parse_args(["--profile", "toaster"], {})

        assert stopped.value.code == 2
        assert "smart-display" in capsys.readouterr().err

    def test_the_help_says_what_each_profile_is(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            parse_args(["--help"], {})

        out = capsys.readouterr().out
        assert "speaker" in out
        assert "a streamer with no microphone" in out

    def test_the_environment_is_the_real_one_when_none_is_given(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(TOKEN_VARIABLE, "from-the-real-environment")

        assert parse_args([]).token == "from-the-real-environment"

    def test_by_default_nothing_is_read_from_files_or_written_to_one(self) -> None:
        options = parse_args([], {})

        assert (options.wav_in, options.wav_out, options.realtime) == ((), None, False)

    def test_recordings_in_files_are_said_in_the_order_they_are_given(self) -> None:
        options = parse_args(
            ["--wav-in", "one.wav", "--wav-out", "answer.wav", "--wav-in", "two.wav"], {}
        )

        assert options.wav_in == (Path("one.wav"), Path("two.wav"))
        assert options.wav_out == Path("answer.wav")

    def test_what_is_said_can_be_paced_as_a_microphone_would(self) -> None:
        assert parse_args(["--wav-in", "one.wav", "--realtime"], {}).realtime is True

    def test_a_voice_and_files_are_two_ways_of_hearing_a_person_and_only_one_is_chosen(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as stopped:
            parse_args(["--voice", "--wav-in", "one.wav"], {})

        assert stopped.value.code == 2
        assert "choose one" in capsys.readouterr().err

    def test_a_pace_is_for_files_and_there_are_none(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as stopped:
            parse_args(["--realtime"], {})

        assert stopped.value.code == 2
        assert "--wav-in" in capsys.readouterr().err

    def test_a_file_to_write_to_goes_with_a_voice_or_with_a_keyboard(self) -> None:
        assert parse_args(["--voice", "--wav-out", "answer.wav"], {}).wav_out == Path("answer.wav")
        assert parse_args(["--wav-out", "answer.wav"], {}).voice is False


class TestConversing:
    async def test_what_is_typed_is_sent_and_the_answer_comes_out_as_the_device_would_do(
        self,
    ) -> None:
        connection = connected("speaker")

        def answer() -> None:
            connection.say(PartFrame(utterance_id="u1", seq=0, part=Speech(text="Ten seconds.")))
            connection.say(TurnEnded(utterance_id="u1", status=TurnStatus.FINISHED))

        code, out, _ = await session(
            "speaker", "how long should I hold it?", answer, connection=connection
        )

        assert code == 0
        assert out == [
            f"{CONNECTED} (audio_in, audio_out, led)",
            INVITATION,
            "[say]    Ten seconds.",
        ]
        assert [u.text for u in connection.frames("utterance")] == ["how long should I hold it?"]

    async def test_a_line_with_nothing_in_it_is_not_said_and_spaces_around_one_do_not_count(
        self,
    ) -> None:
        connection = connected("speaker")

        await session(
            "speaker", "", "   ", "  hello  ", utterance_ended(connection), connection=connection
        )

        assert [u.text for u in connection.frames("utterance")] == ["hello"]

    async def test_what_the_edge_says_is_wrong_is_said_and_the_conversation_goes_on(self) -> None:
        connection = connected("speaker")

        def wrong() -> None:
            connection.say(ErrorFrame(code="rate_limited", message="slow down"))

        code, out, _ = await session(
            "speaker", wrong, "still here", utterance_ended(connection), connection=connection
        )

        assert code == 0
        assert "[edge!]  rate_limited: slow down" in out
        assert [u.text for u in connection.frames("utterance")] == ["still here"]

    async def test_speaking_over_an_answer_is_left_to_the_edge_to_interrupt(
        self,
    ) -> None:
        connection = connected("speaker")

        def the_edge_interrupts() -> None:
            connection.say(TurnEnded(utterance_id="u1", status=TurnStatus.CANCELLED))
            connection.say(TurnEnded(utterance_id="u2", status=TurnStatus.FINISHED))

        code, out, _ = await session(
            "speaker", "one", "two", the_edge_interrupts, connection=connection
        )

        assert code == 0
        assert [(u.id, u.text) for u in connection.frames("utterance")] == [
            ("u1", "one"),
            ("u2", "two"),
        ]
        assert connection.frames("cancel") == []
        assert "[stop]   interrupted" in out

    async def test_what_is_typed_when_it_is_over_waits_for_the_last_answer(self) -> None:
        connection = connected("speaker")

        def answer_in_a_while() -> None:
            def answer() -> None:
                connection.say(PartFrame(utterance_id="u1", seq=0, part=Speech(text="Late.")))
                connection.say(TurnEnded(utterance_id="u1", status=TurnStatus.FINISHED))

            asyncio.get_running_loop().call_later(0.1, answer)

        code, out, _ = await session("speaker", "hello", answer_in_a_while, connection=connection)

        assert code == 0
        assert out[-1] == "[say]    Late."


async def voice_session(
    profile: str,
    *recordings: AudioPayload,
    connection: MemoryConnection | None = None,
    voice_output: FakeVoiceOutput | None = None,
) -> tuple[int, list[str], MemoryConnection, FakeVoiceOutput]:
    """A person at a terminal of this profile who says these recordings, with ``--voice``. What
    comes out is returned as the lines of the screen, and what was played back."""
    connection = connection or connected(profile)
    voice_output = voice_output or FakeVoiceOutput()
    out = io.StringIO()
    code = await run(
        Options(
            url=DEFAULT_URL, profile=get_profile(profile), hardware_id=None, token=None, voice=True
        ),
        out=out,
        open_connection=lambda _url: opened(connection),
        voice_input=FakeVoiceInput(*recordings),
        voice_output=voice_output,
    )
    return code, out.getvalue().splitlines(), connection, voice_output


class TestConversingByVoice:
    async def test_a_wake_word_sends_the_recording_and_the_answer_is_shown_and_played(
        self,
    ) -> None:
        connection = connected("speaker")

        def answer() -> None:
            connection.say(
                PartFrame(
                    utterance_id="u1", seq=0, part=Speech(text="Ten seconds.", audio=RECORDING)
                )
            )
            connection.say(TurnEnded(utterance_id="u1", status=TurnStatus.FINISHED))
            connection.drop()

        asyncio.get_running_loop().call_later(0.05, answer)
        code, out, _, played = await voice_session("speaker", RECORDING, connection=connection)

        assert [u.audio for u in connection.frames("audio_utterance")] == [RECORDING]
        assert "[say]    Ten seconds." in out
        assert played.played == [RECORDING]
        assert code == 1  # the connection ends once the answer is in, same as typing would report

    async def test_a_profile_with_no_microphone_cannot_use_voice(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = await run(
            Options(
                url=DEFAULT_URL,
                profile=get_profile("screen"),
                hardware_id=None,
                token=None,
                voice=True,
            ),
            voice_input=FakeVoiceInput(),
        )

        assert code == 1
        assert "needs a microphone" in capsys.readouterr().err

    async def test_voice_without_an_access_key_and_no_fake_input_is_refused(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = await run(
            Options(
                url=DEFAULT_URL,
                profile=get_profile("speaker"),
                hardware_id=None,
                token=None,
                voice=True,
            )
        )

        assert code == 1
        assert "AccessKey" in capsys.readouterr().err

    async def test_the_edge_dropping_the_connection_is_said_and_voice_mode_ends(self) -> None:
        connection = connected("speaker")
        asyncio.get_running_loop().call_later(0.05, lambda: connection.drop(4002, "idle"))

        code, out, _, _ = await voice_session("speaker", connection=connection)

        assert code == 1
        assert out[-1] == "--       the connection to the edge ended (4002: idle)"


CLIP = wav_of(b"\x05\x00" * 100, 22050)
"""What an answer sounds like, as the gateway's Piper hands it over: a WAV file."""


def a_recording(path: Path, pcm: bytes) -> Path:
    path.write_bytes(wav_of(pcm, 16000).data)
    return path


def an_edge_that_answers(connection: MemoryConnection, *answers: str) -> "asyncio.Task[None]":
    """An edge that, once an utterance has been sent to its end, says what it took it for and
    answers it with speech that has a voice."""

    async def answering() -> None:
        for number, said in enumerate(answers, start=1):
            await connection.wait_for("audio_end", count=number)
            identity = f"u{number}"
            connection.say(Heard(utterance_id=identity, text=f"what was said, {number}"))
            connection.say(
                PartFrame(utterance_id=identity, seq=0, part=Speech(text=said, audio=CLIP))
            )
            connection.say(TurnEnded(utterance_id=identity, status=TurnStatus.FINISHED))

    return asyncio.create_task(answering())


async def from_files(
    profile: str,
    *recordings: Path,
    connection: MemoryConnection,
    wav_out: Path | None = None,
    realtime: bool = False,
) -> tuple[int, list[str]]:
    """A person at a terminal of this profile whose recordings are in files, with ``--wav-in``."""
    out = io.StringIO()
    code = await run(
        Options(
            url=DEFAULT_URL,
            profile=get_profile(profile),
            hardware_id=None,
            token=None,
            wav_in=recordings,
            wav_out=wav_out,
            realtime=realtime,
        ),
        out=out,
        open_connection=lambda _url: opened(connection),
    )
    return code, out.getvalue().splitlines()


def written(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as wav_reader:
        return wav_reader.readframes(wav_reader.getnframes()), wav_reader.getframerate()


class TestConversingFromFiles:
    async def test_the_files_are_said_in_order_each_as_a_stream_and_each_answer_is_waited_for(
        self, tmp_path: Path
    ) -> None:
        first = a_recording(tmp_path / "one.wav", b"\x01\x00" * 1600)
        second = a_recording(tmp_path / "two.wav", b"\x02\x00" * 100)
        connection = connected("speaker")
        answering = an_edge_that_answers(connection, "First answer.", "Second answer.")

        code, out = await from_files("speaker", first, second, connection=connection)
        await answering

        assert code == 0
        assert out[0].startswith(CONNECTED)
        assert out[1:] == [
            "--       saying 2 recordings from files, as if said after the wake word",
            "[heard]  what was said, 1",
            "[say]    First answer.",
            "[heard]  what was said, 2",
            "[say]    Second answer.",
        ]
        frames = connection.frames("audio_start", "audio_chunk", "audio_end")
        assert [(f.type, f.id) for f in frames] == [
            ("audio_start", "u1"),
            ("audio_chunk", "u1"),
            ("audio_end", "u1"),
            ("audio_start", "u2"),
            ("audio_chunk", "u2"),
            ("audio_end", "u2"),
        ]
        assert [c.data for c in frames if c.type == "audio_chunk"] == [
            b"\x01\x00" * 1600,
            b"\x02\x00" * 100,
        ]
        assert connection.frames("audio_utterance") == []  # a stream, and not a clip
        assert len(connection.frames("bye")) == 1  # and when they are said the person leaves

    async def test_one_file_is_one_recording(self, tmp_path: Path) -> None:
        connection = connected("speaker")
        answering = an_edge_that_answers(connection, "Ten seconds.")

        code, out = await from_files(
            "speaker", a_recording(tmp_path / "one.wav", b"\x01\x00" * 100), connection=connection
        )
        await answering

        assert code == 0
        assert "--       saying 1 recording from files, as if said after the wake word" in out

    async def test_what_the_assistant_says_can_be_written_to_a_file_instead_of_played(
        self, tmp_path: Path
    ) -> None:
        question = a_recording(tmp_path / "question.wav", b"\x01\x00" * 100)
        answer = tmp_path / "answer.wav"
        connection = connected("speaker")
        answering = an_edge_that_answers(connection, "First.", "Second.")

        code, out = await from_files(
            "speaker", question, question, connection=connection, wav_out=answer
        )
        await answering

        assert code == 0
        assert written(answer) == (b"\x05\x00" * 200, 22050)  # both answers, one after the other
        assert out[-1] == f"--       wrote {200 / 22050:.1f} s of what was said to {answer}"

    async def test_a_file_that_is_not_written_because_nothing_was_said_is_said(
        self, tmp_path: Path
    ) -> None:
        answer = tmp_path / "answer.wav"
        connection = connected("speaker")
        asyncio.get_running_loop().call_later(0.05, connection.drop)  # nobody answers

        _, out = await from_files(
            "speaker",
            a_recording(tmp_path / "question.wav", b"\x01\x00" * 100),
            connection=connection,
            wav_out=answer,
        )

        assert f"--       nothing was said, so {answer} was not written" in out
        assert not answer.exists()

    async def test_an_utterance_that_the_edge_does_not_take_does_not_hold_up_the_next(
        self, tmp_path: Path
    ) -> None:
        question = a_recording(tmp_path / "question.wav", b"\x01\x00" * 100)
        connection = connected("speaker")

        async def refusing_the_first() -> None:
            await connection.wait_for("audio_start", count=1)
            connection.say(ErrorFrame(code="speech_not_configured", message="no speech", ref="u1"))
            await connection.wait_for("audio_end", count=2)
            connection.say(Heard(utterance_id="u2", text="the second"))
            connection.say(TurnEnded(utterance_id="u2", status=TurnStatus.FINISHED))

        edge = asyncio.create_task(refusing_the_first())

        code, out = await from_files("speaker", question, question, connection=connection)
        await edge

        assert code == 0
        assert "[edge!]  speech_not_configured: no speech" in out
        assert out[-1] == "[heard]  the second"

    async def test_a_file_is_said_in_the_time_it_takes_when_that_is_asked_for(
        self, tmp_path: Path
    ) -> None:
        question = a_recording(tmp_path / "question.wav", bytes(3200 * 2))  # two tenths of a second
        timings: dict[bool, float] = {}

        for realtime in (False, True):
            connection = connected("speaker")
            answering = an_edge_that_answers(connection, "Ten seconds.")
            started = asyncio.get_running_loop().time()
            code, _ = await from_files(
                "speaker", question, connection=connection, realtime=realtime
            )
            timings[realtime] = asyncio.get_running_loop().time() - started
            await answering
            assert code == 0

        assert timings[True] >= 0.15  # what was said takes as long as it did to say
        assert timings[False] < timings[True]

    async def test_a_file_that_is_not_there_is_said_before_the_edge_is_reached(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        async def unreachable(url: str) -> Connection:
            raise AssertionError("the edge was reached")

        code = await run(
            Options(
                url=DEFAULT_URL,
                profile=get_profile("speaker"),
                hardware_id=None,
                token=None,
                wav_in=(tmp_path / "nothing.wav",),
            ),
            open_connection=unreachable,
        )

        assert code == 1
        assert "nothing.wav" in capsys.readouterr().err

    async def test_a_file_that_is_not_a_recording_is_said_with_its_name(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        notes = tmp_path / "notes.wav"
        notes.write_bytes(b"not audio")

        code = await run(
            Options(
                url=DEFAULT_URL,
                profile=get_profile("speaker"),
                hardware_id=None,
                token=None,
                wav_in=(notes,),
            )
        )

        assert code == 1
        error = capsys.readouterr().err
        assert str(notes) in error
        assert "not a WAV file" in error

    async def test_a_profile_with_no_microphone_cannot_say_files(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = await run(
            Options(
                url=DEFAULT_URL,
                profile=get_profile("screen"),
                hardware_id=None,
                token=None,
                wav_in=(a_recording(tmp_path / "question.wav", b"\x01\x00" * 100),),
            )
        )

        assert code == 1
        assert "--wav-in needs a microphone" in capsys.readouterr().err

    async def test_the_edge_dropping_the_connection_is_said_and_the_files_that_are_left_are_not(
        self, tmp_path: Path
    ) -> None:
        question = a_recording(tmp_path / "question.wav", b"\x01\x00" * 100)
        connection = connected("speaker")
        asyncio.get_running_loop().call_later(0.05, lambda: connection.drop(4002, "idle"))

        code, out = await from_files("speaker", question, question, connection=connection)

        assert code == 1
        assert out[-1] == "--       the connection to the edge ended (4002: idle)"
        assert len(connection.frames("audio_start")) == 1

    async def test_a_person_who_has_said_everything_is_not_a_connection_that_was_lost(
        self, tmp_path: Path
    ) -> None:
        connection = connected("speaker")
        answering = an_edge_that_answers(connection, "Ten seconds.")

        code, out = await from_files(
            "speaker", a_recording(tmp_path / "q.wav", b"\x01\x00" * 100), connection=connection
        )
        await answering

        assert code == 0
        assert not any("the connection to the edge ended" in line for line in out)


class TestAFileForWhatIsSaidToAKeyboard:
    async def test_what_the_assistant_says_to_what_is_typed_can_be_written_to_a_file_too(
        self, tmp_path: Path
    ) -> None:
        answer = tmp_path / "answer.wav"
        connection = connected("speaker")

        def says_something() -> None:
            connection.say(
                PartFrame(utterance_id="u1", seq=0, part=Speech(text="Ten seconds.", audio=CLIP))
            )
            connection.say(TurnEnded(utterance_id="u1", status=TurnStatus.FINISHED))

        out = io.StringIO()
        code = await run(
            Options(
                url=DEFAULT_URL,
                profile=get_profile("speaker"),
                hardware_id=None,
                token=None,
                wav_out=answer,
            ),
            lines=Typed("how long should I hold it?", says_something).read(),
            out=out,
            open_connection=lambda _url: opened(connection),
        )

        assert code == 0
        assert written(answer) == (b"\x05\x00" * 100, 22050)
        assert "[say]    Ten seconds." in out.getvalue().splitlines()


class TestTheCommandsOfTheTerminal:
    @pytest.mark.parametrize("quit_", ["/quit", "/exit", "/q", "/QUIT"])
    async def test_it_can_be_left_and_what_comes_after_is_not_said(self, quit_: str) -> None:
        code, _, connection = await session("speaker", quit_, "not said")

        assert code == 0
        assert connection.frames("utterance") == []
        assert len(connection.frames("bye")) == 1

    async def test_it_says_what_the_answer_that_is_coming_can_be_stopped_with(self) -> None:
        connection = connected("speaker")

        def stopped() -> None:
            connection.say(TurnEnded(utterance_id="u1", status=TurnStatus.CANCELLED))

        code, out, _ = await session(
            "speaker", "tell me a long story", "/cancel", stopped, connection=connection
        )

        assert code == 0
        assert [c.id for c in connection.frames("cancel")] == ["u1"]
        assert "[stop]   interrupted" in out

    async def test_stopping_what_is_not_being_answered_is_said_and_nothing_is_sent(self) -> None:
        _, out, connection = await session("speaker", "/cancel")

        assert "-- nothing is being answered" in out
        assert connection.frames("cancel") == []

    async def test_what_the_hardware_is_doing_can_be_asked(self) -> None:
        connection = connected("speaker")

        def louder() -> None:
            connection.say(CommandFrame(id="c1", command=SetVolume(level=30)))

        _, out, _ = await session("speaker", louder, "/state", connection=connection)

        assert "[do]     volume 30" in out
        assert out[-1] == "-- volume 30; light off; music stopped; screen: nothing"
        [ack] = connection.frames("ack")
        assert (ack.id, ack.result.succeeded) == ("c1", True)

    async def test_what_the_device_can_do_can_be_asked(self) -> None:
        _, speaker, _ = await session("speaker", "/caps")
        _, display, _ = await session("smart-display", "/caps")

        assert speaker[-1] == "-- speaker: audio_in, audio_out, led"
        assert display[-1] == "-- smart-display: audio_in, audio_out, display, led; screen 800x480"

    async def test_the_commands_can_be_listed(self) -> None:
        _, out, _ = await session("speaker", "/help")

        assert "\n".join(out[-len(HELP.splitlines()) :]) == HELP

    async def test_a_command_that_does_not_exist_is_said_and_is_not_said_to_the_assistant(
        self,
    ) -> None:
        _, out, connection = await session("speaker", "/dance")

        assert out[-1] == "-- /dance is not a command of the terminal (/help)"
        assert connection.frames("utterance") == []


class TestADeviceThatCannotConverse:
    async def test_it_says_that_it_only_shows_what_it_is_sent_and_says_nothing_of_its_own(
        self,
    ) -> None:
        code, out, connection = await session("screen", "hello?")

        assert code == 0
        assert (
            "--       this device cannot start a conversation: it shows what it is sent (/help)"
            in out
        )
        assert out[-1] == "-- this device has no microphone, so it cannot start a conversation"
        assert connection.frames("utterance") == []

    async def test_what_it_is_sent_it_shows_and_says_that_it_did(self) -> None:
        connection = connected("screen")

        def something_for_the_screen() -> None:
            connection.say(CommandFrame(id="c1", command=ShowCard(card=Card(title="Recipe"))))

        _, out, _ = await session("screen", something_for_the_screen, connection=connection)

        assert "[card]   Recipe" in out
        [ack] = connection.frames("ack")
        assert (ack.id, ack.result.succeeded) == ("c1", True)


class TestWhenTheConnectionEnds:
    async def test_the_edge_closing_it_is_said_at_once_and_not_when_something_is_typed(
        self,
    ) -> None:
        connection = connected("speaker")

        def idle() -> None:
            connection.drop(4002, "idle")

        code, out, _ = await session("speaker", idle, never, connection=connection)

        assert code == 1
        assert out[-1] == "--       the connection to the edge ended (4002: idle)"

    async def test_a_connection_that_ends_with_an_answer_due_does_not_leave_it_waiting(
        self,
    ) -> None:
        connection = connected("speaker")

        def gone_in_a_while() -> None:
            asyncio.get_running_loop().call_later(0.05, connection.drop)

        code, out, _ = await session("speaker", "hello", gone_in_a_while, connection=connection)

        assert code == 1
        assert out[-1] == "--       the connection to the edge ended (1006)"

    async def test_a_connection_that_cannot_be_written_to_is_a_connection_that_was_lost(
        self,
    ) -> None:
        connection = connected("speaker")

        def cut() -> None:
            connection.closed = True

        code, out, _ = await session("speaker", cut, "anyone?", connection=connection)

        assert code == 1
        assert out[-1].startswith("--       the connection to the edge ended")

    async def test_a_person_who_leaves_is_not_a_connection_that_was_lost(self) -> None:
        code, out, _ = await session("speaker", "/quit")

        assert code == 0
        assert not any("the connection to the edge ended" in line for line in out)


class TestWhenThereIsNoEdge:
    async def test_an_edge_that_cannot_be_reached_is_said_on_the_error_output(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        async def unreachable(url: str) -> Connection:
            raise CouldNotConnect(f"could not connect to {url}: refused")

        code = await run(
            Options(url=DEFAULT_URL, profile=get_profile("speaker"), hardware_id=None, token=None),
            lines=Typed().read(),
            open_connection=unreachable,
        )

        assert code == 1
        assert capsys.readouterr().err == (
            f"factotum-terminal: could not connect to {DEFAULT_URL}: refused\n"
        )

    async def test_an_edge_that_does_not_know_the_device_is_said_and_the_connection_is_closed(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        connection = MemoryConnection()
        connection.say(
            ErrorFrame(code="auth_failed", message="the device is not known", fatal=True)
        )

        code = await run(
            Options(url=DEFAULT_URL, profile=get_profile("speaker"), hardware_id=None, token=None),
            lines=Typed().read(),
            out=io.StringIO(),
            open_connection=lambda _url: opened(connection),
        )

        assert code == 1
        assert capsys.readouterr().err == (
            "factotum-terminal: the edge refused this device: "
            "auth_failed: the device is not known\n"
        )
        assert connection.closed


class TestWhatIsTypedAtARealTerminal:
    async def test_it_is_read_a_line_at_a_time_until_it_is_over(self) -> None:
        lines = [line async for line in stdin_lines(io.StringIO("one\ntwo\n"))]

        assert lines == ["one\n", "two\n", None]

    async def test_it_is_the_standard_input_when_no_other_is_given(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO("from the keyboard\n"))

        assert [line async for line in stdin_lines()] == ["from the keyboard\n", None]

    async def test_a_terminal_that_is_given_no_lines_reads_them_from_there(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO("hello\n"))
        connection = connected("speaker")

        def answer() -> None:
            connection.say(PartFrame(utterance_id="u1", seq=0, part=Speech(text="Hi.")))
            connection.say(TurnEnded(utterance_id="u1", status=TurnStatus.FINISHED))

        asyncio.get_running_loop().call_later(0.1, answer)

        code = await run(
            Options(url=DEFAULT_URL, profile=get_profile("speaker"), hardware_id=None, token=None),
            open_connection=lambda _url: opened(connection),
        )

        assert code == 0
        assert capsys.readouterr().out.splitlines()[-1] == "[say]    Hi."
        assert [u.text for u in connection.frames("utterance")] == ["hello"]


class TestTheCommandLine:
    def test_the_exit_code_is_how_the_conversation_went(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[Options] = []

        async def fake_run(options: Options) -> int:
            seen.append(options)
            return 3

        monkeypatch.setattr(cli, "run", fake_run)

        with pytest.raises(SystemExit) as stopped:
            cli.main(["--profile", "phone", "--hardware-id", "phone-7"])

        assert stopped.value.code == 3
        assert (seen[0].profile.name, seen[0].hardware_id) == ("phone", "phone-7")

    def test_ctrl_c_is_a_way_of_leaving_and_not_a_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def interrupted(options: Options) -> int:
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "run", interrupted)

        with pytest.raises(SystemExit) as stopped:
            cli.main([])

        assert stopped.value.code == 0
