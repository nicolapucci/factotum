import asyncio
import logging
from typing import Any
from uuid import uuid4

import pytest

from factotum.contracts.capabilities import Capability, DisplayInfo
from factotum.contracts.cards import Card
from factotum.contracts.commands import (
    CommandResult,
    CommandStatus,
    SetLed,
    SetVolume,
    ShowCard,
)
from factotum.contracts.conversation import (
    Delta,
    Failed,
    Finished,
    Phase,
    ReferenceKind,
    Started,
    Status,
)
from factotum.contracts.edge import (
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    Ack,
    Bye,
    Cancel,
    CloseCode,
    Ping,
    TurnStatus,
    Utterance,
)
from factotum.contracts.errors import CapabilityNotSupportedError, ChatNotConfiguredError
from factotum.contracts.identity import SubjectKind
from factotum.contracts.ids import ConversationId, ExchangeId
from factotum.contracts.response import CardPart, Speech, StatusPart, StatusState, Text
from factotum.edge.session import Session
from factotum.edge.testing import (
    HANG,
    SPEAKER,
    TENANT,
    USER,
    Rig,
    Step,
    answer,
    citation,
    hello,
    make_device,
    quick_settings,
)

LONG_ENOUGH = "Hold the reset button for ten seconds. "


def going_on() -> list[Step]:
    """An answer that has said a sentence and is still being written."""
    return [*answer(LONG_ENOUGH + "Then")[:-1], HANG]


def parts_of(wire_frames: list) -> list:  # type: ignore[type-arg]
    return [frame.part for frame in wire_frames if frame.type == "part"]


class TestSayingHello:
    async def test_a_device_that_is_known_is_told_what_it_is_and_what_it_may_do(self) -> None:
        async with Rig(settings=quick_settings(heartbeat_s=7)) as rig:
            connected = await rig.connect()

            ready = connected.ready
            assert ready.device_id == rig.devices[0][0].id
            assert ready.name == "Kitchen speaker"
            assert ready.protocol_version == PROTOCOL_VERSION
            assert ready.capabilities == frozenset(SPEAKER)
            assert ready.unregistered == frozenset()
            assert ready.heartbeat_s == 7
            assert ready.session_id == connected.session.session_id

    async def test_what_it_declares_beyond_what_it_is_registered_for_is_ignored_and_said(
        self,
    ) -> None:
        async with Rig() as rig:
            connected = await rig.connect(
                capabilities={*SPEAKER, Capability.LED, Capability.DISPLAY}
            )

            assert connected.ready.capabilities == frozenset(SPEAKER)
            assert connected.ready.unregistered == {Capability.LED, Capability.DISPLAY}
            assert connected.session.capabilities == frozenset(SPEAKER)

    async def test_a_device_may_declare_less_than_it_is_registered_for(self) -> None:
        async with Rig() as rig:
            connected = await rig.connect(capabilities={Capability.AUDIO_OUT})  # the mic is off

            assert connected.ready.capabilities == {Capability.AUDIO_OUT}

    async def test_a_screen_is_described_by_the_device_or_else_by_what_it_is_registered_as(
        self,
    ) -> None:
        registered = DisplayInfo(width=800, height=480)
        device = make_device(capabilities=[*SPEAKER, Capability.DISPLAY], display=registered)
        async with Rig(device) as rig:
            plain = await rig.connect()
            own = DisplayInfo(width=400, height=300, touch=True)
            other = await rig.connect(display=own)

            assert plain.ready.display == registered
            assert other.ready.display == own

    async def test_no_screen_is_described_for_a_device_that_may_not_have_one(self) -> None:
        registered = DisplayInfo(width=800, height=480)
        device = make_device(capabilities=[*SPEAKER, Capability.DISPLAY], display=registered)
        async with Rig(device) as rig:
            connected = await rig.connect(capabilities=SPEAKER)

            assert connected.ready.display is None

    async def test_the_edge_knows_the_device_while_it_is_connected_and_not_after(self) -> None:
        async with Rig() as rig:
            connected = await rig.connect()
            key = (TENANT, rig.devices[0][0].id)
            assert rig.sessions.get(*key) is connected.session

            connected.wire.hang_up()
            await connected.task

            assert rig.sessions.get(*key) is None
            assert len(rig.sessions) == 0

    async def test_the_session_is_not_anybodys_until_the_device_has_said_who_it_is(self) -> None:
        async with Rig() as rig:
            session, _, _ = rig.open()

            with pytest.raises(RuntimeError, match="said who it is"):
                _ = session.key

    async def test_being_connected_is_logged_and_the_session_carries_on(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # With the log at INFO, as in a deployment: what is logged must be fit to be logged.
        caplog.set_level(logging.INFO, logger="factotum.edge")
        async with Rig() as rig:
            connected = await rig.connect()
            connected.wire.say(Ping())
            await connected.wire.wait_for("pong")
            connected.wire.hang_up()
            await connected.task

        assert [r.getMessage() for r in caplog.records] == [
            "a device connected",
            "a device disconnected",
        ]
        assert caplog.records[0].device_name == "Kitchen speaker"  # type: ignore[attr-defined]
        assert caplog.records[0].capabilities == ["audio_in", "audio_out"]  # type: ignore[attr-defined]

    async def test_a_device_that_says_hello_twice_is_refused(self) -> None:
        async with Rig() as rig:
            connected = await rig.connect()

            connected.wire.say(hello(rig.devices[0]))
            await connected.task

            [error] = connected.wire.frames("error")
            assert (error.code, error.fatal) == ("invalid_frame", True)
            assert connected.wire.closed_with is not None
            assert connected.wire.closed_with[0] == CloseCode.PROTOCOL_ERROR


class TestWhoIsRefused:
    @pytest.mark.parametrize(
        "said",
        [{"token": "not-the-credential"}, {"hardware_id": "nobody-01"}],
        ids=["a wrong credential", "a device nobody has heard of"],
    )
    async def test_a_device_that_cannot_prove_who_it_is_is_refused_without_a_hint(
        self, said: dict[str, Any]
    ) -> None:
        async with Rig() as rig:
            session, wire, task = rig.open()

            wire.say(hello(rig.devices[0], **said))
            await task

            [error] = wire.frames("error")
            assert (error.code, error.fatal) == ("auth_failed", True)
            assert error.message == "the device is not known, or its credential is wrong"
            assert wire.closed_with is not None
            assert wire.closed_with[0] == CloseCode.POLICY
            assert len(rig.sessions) == 0
            assert wire.frames("ready") == []
            assert session.record is None

    async def test_a_device_that_does_not_say_hello_first_is_refused(self) -> None:
        async with Rig() as rig:
            _, wire, task = rig.open()

            wire.say(Utterance(id="u1", text="hello?"))
            await task

            [error] = wire.frames("error")
            assert error.code == "hello_required"
            assert wire.closed_with is not None
            assert wire.closed_with[0] == CloseCode.POLICY
            assert rig.conversation.turns == []

    async def test_what_is_not_a_frame_is_refused(self) -> None:
        async with Rig() as rig:
            _, wire, task = rig.open()

            wire.say("this is not json")
            await task

            [error] = wire.frames("error")
            assert error.code == "invalid_frame"
            assert wire.closed_with is not None
            assert wire.closed_with[0] == CloseCode.PROTOCOL_ERROR

    async def test_a_device_that_speaks_another_version_is_told_which_the_edge_speaks(self) -> None:
        async with Rig() as rig:
            _, wire, task = rig.open()

            wire.say(hello(rig.devices[0], protocol_version=PROTOCOL_VERSION + 1))
            await task

            [error] = wire.frames("error")
            assert error.code == "unsupported_protocol"
            assert str(PROTOCOL_VERSION) in error.message
            assert wire.closed_with is not None
            assert wire.closed_with[0] == CloseCode.UNSUPPORTED_VERSION

    async def test_a_device_that_says_nothing_is_not_waited_for(self) -> None:
        async with Rig(settings=quick_settings(hello_timeout_s=0.05)) as rig:
            _, wire, task = rig.open()

            await task

            [error] = wire.frames("error")
            assert error.code == "hello_required"
            assert wire.closed_with is not None
            assert wire.closed_with[0] == CloseCode.POLICY

    async def test_a_device_that_is_refused_is_logged_with_the_hardware_id_it_gave_and_why(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="factotum.edge")
        async with Rig() as rig:
            _, wire, task = rig.open()
            wire.say(hello(rig.devices[0], token="not-the-credential"))
            await task

        [record] = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert record.getMessage() == "a device was refused"
        assert record.code == "auth_failed"  # type: ignore[attr-defined]
        assert record.claimed_hardware_id == "kitchen-01"  # type: ignore[attr-defined]
        assert "not-the-credential" not in caplog.text

    async def test_a_device_that_is_sent_away_after_it_was_known_is_logged_as_such(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger="factotum.edge")
        async with Rig(settings=quick_settings(idle_timeout_s=0.05)) as rig:
            connected = await rig.connect()
            await connected.task

        [record] = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert record.getMessage() == "a device was sent away"
        assert record.code == "idle"  # type: ignore[attr-defined]
        assert not hasattr(record, "claimed_hardware_id")

    async def test_the_credential_is_in_no_log(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG)
        secret = "a-secret-credential-that-must-not-leak"
        device = make_device(token=secret)
        async with Rig(device) as rig:
            known = await rig.connect()
            _, wire, task = rig.open()
            wire.say(hello(device, token=secret + "-wrong"))
            await task
            known.wire.hang_up()
            await known.task

        assert secret not in caplog.text
        assert secret not in "".join(text for text in wire.sent)


class TestAnswering:
    async def test_an_utterance_is_answered_in_parts_a_speaker_can_say_and_then_it_is_over(
        self,
    ) -> None:
        async with Rig() as rig:
            connected = await rig.connect()
            rig.conversation.scripts.append(answer(LONG_ENOUGH, "Then let it go. And wait."))

            connected.wire.say(Utterance(id="u1", text="how do I reset the router?"))
            [ended] = await connected.wire.wait_for("turn_ended")

            frames = connected.wire.frames("part", "turn_ended")
            assert [f.seq for f in frames if f.type == "part"] == [0, 1, 2]
            assert all(f.utterance_id == "u1" for f in frames)
            assert [type(p) for p in parts_of(frames)] == [StatusPart, Speech, Speech]
            assert [p.text for p in parts_of(frames) if isinstance(p, Speech)] == [
                "Hold the reset button for ten seconds.",
                "Then let it go. And wait.",
            ]
            assert frames[-1] == ended
            assert (ended.utterance_id, ended.status) == ("u1", TurnStatus.FINISHED)

    async def test_the_conversation_is_asked_for_what_the_device_acts_for(self) -> None:
        async with Rig(make_device(shared=True)) as rig:
            connected = await rig.connect()

            connected.wire.say(Utterance(id="u1", text="  what time is it?  "))
            await connected.wire.wait_for("turn_ended")

            [(principal, message)] = rig.conversation.turns
            assert message == "what time is it?"
            assert principal.acts_as.kind is SubjectKind.TENANT
            assert principal.device_id == rig.devices[0][0].id

    async def test_a_personal_device_is_answered_as_its_owner(self) -> None:
        async with Rig(make_device(shared=False)) as rig:
            connected = await rig.connect()

            connected.wire.say(Utterance(id="u1", text="what is on my calendar?"))
            await connected.wire.wait_for("turn_ended")

            [(principal, _)] = rig.conversation.turns
            assert (principal.acts_as.kind, principal.acts_as.id) == (SubjectKind.USER, USER)

    async def test_a_screen_is_sent_the_text_and_the_sources_as_a_card(self) -> None:
        screen = make_device(capabilities=[Capability.AUDIO_IN, Capability.DISPLAY])
        async with Rig(screen) as rig:
            connected = await rig.connect()
            rig.conversation.scripts.append(
                answer(LONG_ENOUGH + "[1]", sources=[citation(1, "Router manual", 12)])
            )

            connected.wire.say(Utterance(id="u1", text="how do I reset the router?"))
            await connected.wire.wait_for("turn_ended")

            parts = parts_of(connected.wire.frames("part"))
            assert [type(p) for p in parts] == [StatusPart, Text, CardPart]
            assert parts[1].text == "Hold the reset button for ten seconds. [1]"
            assert parts[2].card.items[0].label == "[1] Router manual"

    async def test_a_device_with_no_microphone_cannot_start_a_conversation_and_stays_connected(
        self,
    ) -> None:
        player = make_device(capabilities=[Capability.AUDIO_OUT])
        async with Rig(player) as rig:
            connected = await rig.connect()

            connected.wire.say(Utterance(id="u1", text="hello?"))
            [error] = await connected.wire.wait_for("error")
            connected.wire.say(Ping())
            await connected.wire.wait_for("pong")

            assert (error.code, error.fatal, error.ref) == ("capability_missing", False, "u1")
            assert rig.conversation.turns == []
            assert connected.wire.closed_with is None

    async def test_the_last_things_said_is_answered_and_the_one_before_is_given_up_on(self) -> None:
        async with Rig() as rig:
            connected = await rig.connect()
            rig.conversation.scripts.append(going_on())
            rig.conversation.scripts.append(answer("A second answer, in full."))

            connected.wire.say(Utterance(id="u1", text="how do I reset the router?"))
            await connected.wire.wait_for("part", count=2)  # it is being answered
            connected.wire.say(Utterance(id="u2", text="never mind, what time is it?"))
            ended = await connected.wire.wait_for("turn_ended", count=2)

            assert [(e.utterance_id, e.status) for e in ended] == [
                ("u1", TurnStatus.CANCELLED),
                ("u2", TurnStatus.FINISHED),
            ]
            second = [f for f in connected.wire.frames("part") if f.utterance_id == "u2"]
            assert [f.seq for f in second] == [0, 1]
            assert rig.conversation.abandoned == 1  # the model of the first is not kept writing
            assert rig.conversation.open == 0

    async def test_a_cancel_stops_the_answer_and_a_later_utterance_is_answered_as_usual(
        self,
    ) -> None:
        async with Rig() as rig:
            connected = await rig.connect()
            rig.conversation.scripts.append(going_on())

            connected.wire.say(Utterance(id="u1", text="how do I reset the router?"))
            await connected.wire.wait_for("part", count=2)
            connected.wire.say(Cancel(id="u1"))
            [ended] = await connected.wire.wait_for("turn_ended")
            connected.wire.say(Utterance(id="u2", text="thanks"))
            await connected.wire.wait_for("turn_ended", count=2)

            assert (ended.utterance_id, ended.status) == ("u1", TurnStatus.CANCELLED)
            assert rig.conversation.abandoned == 1

    async def test_a_cancel_for_what_is_not_being_answered_is_ignored(self) -> None:
        async with Rig() as rig:
            connected = await rig.connect()
            connected.wire.say(Utterance(id="u1", text="hello"))
            await connected.wire.wait_for("turn_ended")
            before = len(connected.wire.sent)

            connected.wire.say(Cancel(id="u1"))  # already over
            connected.wire.say(Cancel(id="never-was"))
            connected.wire.say(Ping())
            await connected.wire.wait_for("pong")

            assert len(connected.wire.sent) == before + 1  # only the pong


class TestWhenAnAnswerFails:
    async def test_what_cannot_be_answered_at_all_is_said_and_its_reason_is_given(self) -> None:
        async with Rig() as rig:
            connected = await rig.connect()
            rig.conversation.scripts.append([ChatNotConfiguredError("no chat model is set up")])

            connected.wire.say(Utterance(id="u1", text="hello"))
            [ended] = await connected.wire.wait_for("turn_ended")

            [speech] = parts_of(connected.wire.frames("part"))
            assert speech == Speech(text="I have no model to answer with yet.")
            assert (ended.status, ended.code, ended.message) == (
                TurnStatus.FAILED,
                "chat_not_configured",
                "no chat model is set up",
            )

    async def test_an_answer_that_fails_along_the_way_keeps_what_was_said_and_says_it_stops(
        self,
    ) -> None:
        async with Rig() as rig:
            connected = await rig.connect()
            rig.conversation.scripts.append(
                [
                    Started(
                        conversation_id=ConversationId(uuid4()), exchange_id=ExchangeId(uuid4())
                    ),
                    Delta(text=LONG_ENOUGH),
                    Failed(code="gateway_unavailable", message="the model is away"),
                ]
            )

            connected.wire.say(Utterance(id="u1", text="how do I reset the router?"))
            [ended] = await connected.wire.wait_for("turn_ended")

            assert [p.text for p in parts_of(connected.wire.frames("part"))] == [
                "Hold the reset button for ten seconds.",
                "Sorry, I could not answer that.",
            ]
            assert (ended.status, ended.code) == (TurnStatus.FAILED, "gateway_unavailable")

    async def test_a_bug_is_logged_and_never_shown_and_the_device_stays_connected(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        async with Rig() as rig:
            connected = await rig.connect()
            rig.conversation.scripts.append([RuntimeError("secret detail of the bug")])

            connected.wire.say(Utterance(id="u1", text="hello"))
            [ended] = await connected.wire.wait_for("turn_ended")
            connected.wire.say(Ping())
            await connected.wire.wait_for("pong")

            assert (ended.status, ended.code, ended.message) == (
                TurnStatus.FAILED,
                "internal_error",
                "the answer failed",
            )
            assert "secret detail" not in "".join(connected.wire.sent)
            assert "secret detail of the bug" in caplog.text  # in the logs, for whoever fixes it

    async def test_a_device_that_goes_away_mid_answer_takes_the_answer_with_it(self) -> None:
        async with Rig() as rig:
            connected = await rig.connect()
            rig.conversation.scripts.append(going_on())

            connected.wire.say(Utterance(id="u1", text="how do I reset the router?"))
            await connected.wire.wait_for("part", count=2)
            connected.wire.hang_up()
            await connected.task

            assert rig.conversation.abandoned == 1
            assert rig.conversation.open == 0
            assert len(rig.sessions) == 0

    async def test_a_device_that_cannot_be_written_to_is_gone(self) -> None:
        async with Rig() as rig:
            connected = await rig.connect()
            rig.conversation.scripts.append(going_on())
            connected.wire.say(Utterance(id="u1", text="how do I reset the router?"))
            await connected.wire.wait_for("part", count=2)

            connected.wire.send_fails = True
            connected.wire.say(Utterance(id="u2", text="and again?"))
            connected.wire.hang_up()
            await connected.task

            assert rig.conversation.open == 0
            assert len(rig.sessions) == 0


class TestKeepingInTouch:
    async def test_a_ping_is_answered_with_a_pong(self) -> None:
        async with Rig() as rig:
            connected = await rig.connect()

            connected.wire.say(Ping())

            assert len(await connected.wire.wait_for("pong")) == 1

    async def test_a_device_that_says_nothing_for_too_long_is_gone(self) -> None:
        async with Rig(settings=quick_settings(idle_timeout_s=0.05)) as rig:
            connected = await rig.connect()

            await connected.task

            [error] = connected.wire.frames("error")
            assert (error.code, error.fatal) == ("idle", True)
            assert connected.wire.closed_with is not None
            assert connected.wire.closed_with[0] == CloseCode.IDLE
            assert len(rig.sessions) == 0

    async def test_a_device_that_keeps_saying_it_is_there_is_not_idle(self) -> None:
        async with Rig(settings=quick_settings(idle_timeout_s=0.12)) as rig:
            connected = await rig.connect()

            for _ in range(6):
                await asyncio.sleep(0.04)
                connected.wire.say(Ping())
            await connected.wire.wait_for("pong", count=6)

            assert connected.wire.closed_with is None
            assert len(rig.sessions) == 1

    async def test_a_device_that_says_goodbye_is_let_go(self) -> None:
        async with Rig() as rig:
            connected = await rig.connect()

            connected.wire.say(Bye())
            await connected.task

            assert connected.wire.closed_with is not None
            assert connected.wire.closed_with[0] == CloseCode.NORMAL
            assert len(rig.sessions) == 0

    @pytest.mark.parametrize(
        "junk",
        [
            pytest.param("not json", id="not json"),
            pytest.param("{}", id="an empty object"),
            pytest.param('{"type": "dance"}', id="a type nobody knows"),
            pytest.param("x" * (MAX_FRAME_BYTES + 1), id="too big"),
        ],
    )
    async def test_what_is_not_a_frame_after_hello_ends_the_connection(self, junk: str) -> None:
        async with Rig() as rig:
            connected = await rig.connect()

            connected.wire.say(junk)
            await connected.task

            [error] = connected.wire.frames("error")
            assert (error.code, error.fatal) == ("invalid_frame", True)
            assert connected.wire.closed_with is not None
            assert connected.wire.closed_with[0] == CloseCode.PROTOCOL_ERROR
            assert len(rig.sessions) == 0


class TestOneConnectionForEachDevice:
    async def test_a_device_that_connects_again_replaces_the_connection_it_had(self) -> None:
        async with Rig() as rig:
            old = await rig.connect()
            rig.conversation.scripts.append(going_on())
            old.wire.say(Utterance(id="u1", text="how do I reset the router?"))
            await old.wire.wait_for("part", count=2)

            new = await rig.connect()
            await old.task

            assert old.wire.closed_with is not None
            assert old.wire.closed_with[0] == CloseCode.REPLACED
            assert rig.sessions.get(TENANT, rig.devices[0][0].id) is new.session
            assert len(rig.sessions) == 1
            assert rig.conversation.abandoned == 1  # what was being said to the old one stops

    async def test_the_one_that_went_does_not_take_the_new_one_with_it(self) -> None:
        async with Rig() as rig:
            old = await rig.connect()
            new = await rig.connect()
            await old.task

            new.wire.say(Ping())
            await new.wire.wait_for("pong")

            assert rig.sessions.get(TENANT, rig.devices[0][0].id) is new.session

    async def test_two_devices_are_two_connections(self) -> None:
        kitchen = make_device()
        bedroom = make_device("Bedroom speaker", "bedroom-01", "bedroom-credential")
        async with Rig(kitchen, bedroom) as rig:
            await rig.connect(kitchen)
            await rig.connect(bedroom)

            assert [d.name for d in rig.sessions.connected(TENANT)] == [
                "Bedroom speaker",
                "Kitchen speaker",
            ]
            assert rig.sessions.connected(uuid4()) == []  # type: ignore[arg-type]

    async def test_when_the_edge_stops_every_device_is_told_it_is_going_away(self) -> None:
        kitchen = make_device()
        bedroom = make_device("Bedroom speaker", "bedroom-01", "bedroom-credential")
        async with Rig(kitchen, bedroom) as rig:
            one = await rig.connect(kitchen)
            two = await rig.connect(bedroom)

            await rig.sessions.close_all()
            await one.task
            await two.task

            assert one.wire.closed_with is not None
            assert one.wire.closed_with[0] == CloseCode.GOING_AWAY
            assert two.wire.closed_with is not None
            assert two.wire.closed_with[0] == CloseCode.GOING_AWAY


def command_of(session: Session, command: object) -> "asyncio.Task[CommandResult]":
    return asyncio.create_task(session.command(command))  # type: ignore[arg-type]


class TestCommandsForADevice:
    async def test_a_command_is_sent_and_what_the_device_answers_is_the_result(self) -> None:
        async with Rig() as rig:
            connected = await rig.connect()

            asked = command_of(connected.session, SetVolume(level=30))
            [sent] = await connected.wire.wait_for("command")
            connected.wire.say(Ack(id=sent.id, result=CommandResult.ok()))

            assert await asked == CommandResult.ok()
            assert sent.command == SetVolume(level=30)

    async def test_a_device_that_could_not_do_it_says_so(self) -> None:
        async with Rig() as rig:
            connected = await rig.connect()
            failed = CommandResult(status=CommandStatus.FAILED, detail="the amplifier is off")

            asked = command_of(connected.session, SetVolume(level=30))
            [sent] = await connected.wire.wait_for("command")
            connected.wire.say(Ack(id=sent.id, result=failed))

            assert await asked == failed

    async def test_a_device_that_does_not_answer_is_reported_as_not_answering(self) -> None:
        async with Rig(settings=quick_settings(command_timeout_s=0.05)) as rig:
            connected = await rig.connect()

            result = await connected.session.command(SetVolume(level=30))

            assert result.status is CommandStatus.TIMEOUT

    async def test_an_answer_that_comes_too_late_is_of_no_use_and_does_no_harm(self) -> None:
        async with Rig(settings=quick_settings(command_timeout_s=0.05)) as rig:
            connected = await rig.connect()
            await connected.session.command(SetVolume(level=30))
            [sent] = await connected.wire.wait_for("command")

            connected.wire.say(Ack(id=sent.id, result=CommandResult.ok()))
            connected.wire.say(Ack(id="never-asked", result=CommandResult.ok()))
            connected.wire.say(Ping())

            assert len(await connected.wire.wait_for("pong")) == 1

    async def test_what_the_device_cannot_do_is_not_even_sent(self) -> None:
        async with Rig() as rig:
            connected = await rig.connect()

            result = await connected.session.command(SetLed(pattern="solid"))  # type: ignore[arg-type]

            assert result.status is CommandStatus.UNSUPPORTED
            assert "led" in (result.detail or "")
            assert connected.wire.frames("command") == []

    async def test_a_device_that_goes_while_it_is_asked_is_offline(self) -> None:
        async with Rig() as rig:
            connected = await rig.connect()

            asked = command_of(connected.session, SetVolume(level=30))
            await connected.wire.wait_for("command")
            connected.wire.hang_up()

            assert (await asked).status is CommandStatus.OFFLINE

    async def test_a_device_that_has_gone_is_offline_and_nothing_is_sent(self) -> None:
        async with Rig() as rig:
            connected = await rig.connect()
            connected.wire.hang_up()
            await connected.task

            result = await connected.session.command(SetVolume(level=30))

            assert result.status is CommandStatus.OFFLINE

    async def test_several_commands_at_once_each_get_their_own_answer(self) -> None:
        async with Rig() as rig:
            connected = await rig.connect()

            first = command_of(connected.session, SetVolume(level=10))
            second = command_of(connected.session, SetVolume(level=20))
            one, two = await connected.wire.wait_for("command", count=2)
            failed = CommandResult(status=CommandStatus.FAILED)
            connected.wire.say(Ack(id=two.id, result=failed))  # out of order
            connected.wire.say(Ack(id=one.id, result=CommandResult.ok()))

            assert (await first, await second) == (CommandResult.ok(), failed)
            assert one.id != two.id


class TestTheDeviceAsThePlatformHoldsIt:
    async def test_its_methods_reach_the_device_and_wait_for_its_answer(self) -> None:
        from factotum.edge.router import EdgeDevice

        async with Rig() as rig:
            connected = await rig.connect()
            device = EdgeDevice(rig.sessions, rig.devices[0][0])

            spoken = asyncio.create_task(device.speak("Dinner is ready"))
            [sent] = await connected.wire.wait_for("command")
            connected.wire.say(Ack(id=sent.id, result=CommandResult.ok()))

            assert await spoken == CommandResult.ok()
            assert sent.command.name == "speak"

    async def test_a_device_that_is_not_connected_is_offline(self) -> None:
        from factotum.edge.router import EdgeDevice

        async with Rig() as rig:
            device = EdgeDevice(rig.sessions, rig.devices[0][0])

            assert (await device.set_volume(20)).status is CommandStatus.OFFLINE

    async def test_a_device_is_not_asked_what_it_is_not_registered_for(self) -> None:
        from factotum.edge.router import EdgeDevice

        async with Rig() as rig:
            connected = await rig.connect()
            device = EdgeDevice(rig.sessions, rig.devices[0][0])

            with pytest.raises(CapabilityNotSupportedError):
                await device.show_card(Card(title="Recipe"))
            assert connected.wire.frames("command") == []

    async def test_a_card_reaches_a_screen(self) -> None:
        from factotum.edge.router import EdgeDevice

        screen = make_device(capabilities=[Capability.DISPLAY])
        async with Rig(screen) as rig:
            connected = await rig.connect()
            device = EdgeDevice(rig.sessions, screen[0])

            shown = asyncio.create_task(device.execute(ShowCard(card=Card(title="Recipe"))))
            [sent] = await connected.wire.wait_for("command")
            connected.wire.say(Ack(id=sent.id, result=CommandResult.ok()))

            assert (await shown).succeeded
            assert sent.command.name == "show_card"


def test_the_reference_of_an_answer_that_needed_no_past_is_none() -> None:
    finished = answer("Hello there, a sentence long enough. ")[-1]

    assert isinstance(finished, Finished)
    assert finished.reference is ReferenceKind.NONE
    assert Status(phase=Phase.ANSWERING).phase is Phase.ANSWERING
    assert StatusState.THINKING.value == "thinking"
