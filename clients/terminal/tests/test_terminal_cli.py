import asyncio
import io
import sys
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest

from factotum.contracts.cards import Card
from factotum.contracts.commands import SetVolume, ShowCard
from factotum.contracts.edge import (
    PROTOCOL_VERSION,
    CommandFrame,
    ErrorFrame,
    PartFrame,
    Ready,
    TurnEnded,
    TurnStatus,
)
from factotum.contracts.ids import DeviceId
from factotum.contracts.response import Speech
from factotum.terminal import cli
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
from factotum.terminal.testing import MemoryConnection, Typed, never

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
